"""Lazy, local-only adapter for Justin's Digital Brain notebook experiment."""

from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from .config import private_directory
from .models import ContractError, canonical_subject, identity


def native_renderer_problem(package_file):
    if not package_file:
        return None
    package_root = Path(package_file).resolve().parent.parent
    renderer = package_root / "metal" / "mglMetal.app" / "Contents" / "MacOS" / "mglMetal"
    command_types = package_root / "metal" / "mglCommandTypes.h"
    if renderer.is_file() and command_types.is_file():
        return None
    return "PGL's native renderer is missing. Reinstall the pinned PGL package before starting a test."


@dataclass(frozen=True)
class RunSettings:
    day: int = 1
    block: int = 1
    description_seconds: int = 12
    display_width: int = 50
    settings_name: str | None = None
    display_name: str | None = None

    def __post_init__(self):
        for value, lower, upper in ((self.day, 1, 365), (self.block, 1, 1000),
                                    (self.description_seconds, 0, 600), (self.display_width, 1, 180)):
            if type(value) is not int or not lower <= value <= upper:
                raise ContractError("Invalid pilot timing, day, block, or display settings")
        for value in (self.settings_name, self.display_name):
            if value is not None and (type(value) is not str or not value.isprintable() or len(value) > 160):
                raise ContractError("Invalid PGL settings name")


@dataclass(frozen=True)
class AdapterResult:
    native_saved: bool
    native_path: Path | None
    error: str | None


class PglAdapter:
    def __init__(self, *, module=None, engine=None):
        self.module = module
        self.engine = engine

    def preflight(self):
        if self.module is None:
            if sys.platform != "darwin" or sys.version_info < (3, 12):
                raise ContractError("PGL execution needs macOS and Python 3.12+; preparation works on Python 3.11+")
            try:
                import pgl
            except (ImportError, OSError, SyntaxError):
                raise ContractError("Install the pinned experiment extra and PGL native prerequisites first") from None
            self.module = pgl
        renderer_problem = native_renderer_problem(getattr(self.module, "__file__", None))
        if renderer_problem:
            raise ContractError(renderer_problem)
        try:
            parameters = inspect.signature(self.module.pglDigitalBrainConfigure).parameters
        except (AttributeError, TypeError, ValueError):
            raise ContractError("PGL Digital Brain configuration is unavailable") from None
        if not {"moviePath", "event_callback"} <= set(parameters):
            raise ContractError("Installed PGL lacks the prepared-media and durable-event hooks; install the pinned fork")

    def _completed(self, experiment):
        state = experiment.state
        phases = getattr(state, "phaseNums", None)
        if (not phases or getattr(state, "runFinishedWithError", True)
                or not getattr(state, "experimentDone", False)
                or getattr(state, "phaseNum", None) != max(phases)):
            return False
        calibration = getattr(self.module, "pglEyeTrackingCalibrationTask", None)
        for task in experiment.tasks:
            if task.data.startTime is None or task.data.endTime is None:
                return False
            skipped = (isinstance(calibration, type) and isinstance(task, calibration)
                       and getattr(task.settings.config, "hasEyeTracker", None) is False)
            if not skipped and task.state.currentTrial < task.settings.nTrials:
                return False
        return True

    def run(self, prepared_root, output_root, subject, attempt_id, callback, settings):
        self.preflight()
        subject = canonical_subject(subject)
        pgl_subject = f"s{int(subject[-3:]):04d}"
        identity(attempt_id)
        output_root = private_directory(output_root)
        engine = self.engine
        experiment = None
        error = None
        old_umask = os.umask(0o077)
        base = self.module.pglExperiment

        class CapturedExperiment(base):
            save_attempted = False
            native_saved = False
            native_path = None
            close_attempted = False

            def save(inner):
                if inner.save_attempted:
                    return
                inner.save_attempted = True
                expected = (output_root / inner.experimentSettings.experimentSaveName / pgl_subject
                            / inner.experimentSettings.sessionName / inner.experimentSettings.runName)
                if not expected.resolve().is_relative_to(output_root) or expected.exists():
                    raise ContractError("Native result destination is not a fresh private path")
                super().save()
                inner.native_path = expected
                required = [expected / name for name in
                            ("experimentSettings.json", "pgl.json", "settings.json", "state.json", "data.json")]
                for task in inner.tasks:
                    required.extend(expected / task.getTaskDirectoryName() / name
                                    for name in ("settings.json", "state.json", "data.json"))
                if not all(path.is_file() and not path.is_symlink() and path.stat().st_size > 0 for path in required):
                    raise ContractError("PGL did not save the required native result files")
                inner.native_saved = True

            def endScreen(inner):
                if inner.close_attempted:
                    return
                inner.close_attempted = True
                super().endScreen()

        try:
            if engine is None:
                engine = self.module.pgl()
            kwargs = {"subjectID": pgl_subject, "experimentName": "DBP integration pilot",
                      "sessionName": f"day{settings.day}", "runName": attempt_id}
            if settings.settings_name is not None:
                kwargs["settingsName"] = settings.settings_name
            if settings.display_name is not None:
                kwargs["displayName"] = settings.display_name
            experiment = CapturedExperiment(engine, **kwargs)
            if not experiment.isInitialized:
                raise ContractError("PGL experiment initialization failed")
            experiment.settings.dataPath = str(output_root)
            experiment.settings.closeScreenOnEnd = True
            current = SimpleNamespace(subjectID=subject, subjectNum=int(subject[-3:]),
                                      dayNum=settings.day, blockNum=settings.block,
                                      descriptionLength=settings.description_seconds, displayWidth=settings.display_width)
            self.module.pglDigitalBrainConfigure(experiment, current, moviePath=Path(prepared_root),
                                                event_callback=callback)
            experiment.initScreen()
            if not experiment.state.openScreen:
                raise ContractError("PGL display did not open")
            experiment.run()
            if not self._completed(experiment):
                error = "IncompleteExperiment"
        except (Exception, KeyboardInterrupt) as caught:
            error = type(caught).__name__
        finally:
            try:
                if experiment is not None and experiment.isInitialized:
                    tracker = getattr(experiment, "eyeTracker", None)
                    if tracker is not None and error is not None:
                        tracker.stop()
                    if not experiment.save_attempted:
                        experiment.save()
            except (Exception, KeyboardInterrupt) as caught:
                error = error or type(caught).__name__
            finally:
                try:
                    if experiment is not None and experiment.isInitialized:
                        experiment.endScreen()
                except (Exception, KeyboardInterrupt) as caught:
                    error = error or type(caught).__name__
                finally:
                    try:
                        if engine is not None:
                            engine.close()
                    except (Exception, KeyboardInterrupt) as caught:
                        error = error or type(caught).__name__
                    os.umask(old_umask)
        return AdapterResult(bool(experiment and experiment.native_saved),
                             experiment.native_path if experiment is not None else None, error)
