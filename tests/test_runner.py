from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbp_pgl_runner.config import save_pairing
from dbp_pgl_runner.runner import StudyRunner
from tests.fixtures import TOKEN, device
from tests.http_fixture import server
from tests.test_api import json_response


class RunnerTests(unittest.TestCase):
    def test_pairing_identity_is_offline_and_does_not_read_or_return_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "selected"
            config = save_pairing(config_dir, "https://example.org", device(), allow_file_token=True)
            (config_dir / config.token_ref).unlink()
            runner = StudyRunner(config_dir=config_dir)
            with patch("dbp_pgl_runner.api._json", side_effect=AssertionError("unexpected network")):
                self.assertEqual(runner.pairing_identity(), {
                    "server_origin": "https://example.org", "device_id": "d" * 32,
                    "experiment_id": "b" * 32,
                })
                replacement = device()
                replacement["device_id"] = "a" * 32
                save_pairing(config_dir, "https://changed.example", replacement, allow_file_token=True)
                self.assertEqual(runner.pairing_identity(), {
                    "server_origin": "https://changed.example", "device_id": "a" * 32,
                    "experiment_id": "b" * 32,
                })

    def test_study_uses_selected_configuration_and_authenticated_endpoint(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
                    "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Pilot",
                    "subjects": [{"subject_id": "subject-001", "trial_count": 12}]}
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "selected"
            with server(lambda *args: json_response(document)) as (origin, requests):
                save_pairing(config_dir, origin, device(), allow_file_token=True)
                self.assertEqual(StudyRunner(config_dir=config_dir).study(), document)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0:2], ("GET", "/api/runner-device/study"))
            self.assertEqual(requests[0][2]["Authorization"], "Bearer " + TOKEN)
