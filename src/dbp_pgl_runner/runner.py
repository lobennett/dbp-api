"""Notebook-friendly entry point using the same guarded workflow as the CLI."""

from pathlib import Path

from .api import ApiError, RunnerApi
from .config import RunnerConfig, preflight_config_directory, save_pairing
from .prepare import prepare_subject, status_subject
from .models import ContractError
from .workflow import attempt_status, recover_subject, run_subject, sync_subject


class StudyRunner:
    def __init__(self, *, config_dir=None, cache_root=None, work_root=None):
        self.config_dir = Path(config_dir or Path.home() / ".config/dbp-pgl")
        self.cache_root = Path(cache_root or Path.home() / ".local/share/dbp-pgl/cache")
        self.work_root = Path(work_root or Path.home() / ".local/share/dbp-pgl/work")

    def connect(self, origin, pairing_code, device_name, *, allow_file_token=False):
        preflight_config_directory(self.config_dir)
        if not allow_file_token:
            from .models import ContractError
            raise ContractError("Explicit private-file credential storage consent is required")
        response = RunnerApi.pair(origin, pairing_code, device_name)
        config = save_pairing(self.config_dir, origin, response, allow_file_token=True)
        return {"device_id": config.device_id, "experiment_id": config.experiment_id}

    def _connection(self):
        config = RunnerConfig.load(self.config_dir)
        return config, RunnerApi(config, config.read_token(self.config_dir))

    def prepare(self, subject):
        config, api = self._connection()
        api.identity()
        return prepare_subject(api, config, subject, self.cache_root, self.work_root)

    def status(self, subject):
        config = RunnerConfig.load(self.config_dir)
        try:
            prepared = status_subject(config, subject, self.work_root)
        except ContractError:
            saved = attempt_status(config, subject, self.work_root)
            return {"preparation_ready": False, "pgl_ready": False, "attempt": saved,
                    "preparation_error": "Prepared stimuli are missing or invalid; saved results remain inspectable and syncable"}
        result = {"preparation_ready": True, "pgl_ready": False,
                  "subject_id": prepared.package.subject_id, "package_id": prepared.package.package_id,
                  "trial_count": len(prepared.package.trials), "prepared_root": str(prepared.root)}
        if (prepared.root.parent / "attempts" / "current.json").exists():
            result["attempt"] = attempt_status(config, subject, self.work_root)
        return result

    def run(self, subject, *, integration_test=False, synchronize=True, prepare=True, **options):
        from .models import ContractError
        if integration_test is not True:
            raise ContractError("Non-participant integration_test=True is required")
        if prepare:
            self.prepare(subject)
        config, api = self._connection()
        result = run_subject(api, config, subject, self.work_root,
                             integration_test=True, **options)
        if synchronize:
            try:
                result = sync_subject(api, config, subject, self.work_root)
            except (ApiError, OSError, ContractError):
                result = {**result, "sync_status": "pending", "next_step": "Results remain local. Run dbp-pgl sync for this subject; do not rerun presentation."}
        return result

    def sync(self, subject):
        config, api = self._connection()
        return sync_subject(api, config, subject, self.work_root)

    def recover(self, subject, *, terminate=False, repair_tail=False):
        return recover_subject(RunnerConfig.load(self.config_dir), subject, self.work_root,
                               terminate=terminate, repair_tail=repair_tail)
