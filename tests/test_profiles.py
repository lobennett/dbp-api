from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from dbp_pgl_runner.api import ApiError
from dbp_pgl_runner.config import RunnerConfig, save_pairing
from dbp_pgl_runner.models import ContractError
from dbp_pgl_runner.profiles import ConnectionProfiles
from tests.fixtures import TOKEN, device
from tests.http_fixture import server
from tests.test_api import json_response


def study_context(name="Practice study", experiment_id="b" * 32):
    return {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
            "experiment_id": experiment_id, "study_id": "c" * 32, "study_name": name,
            "subjects": [{"subject_id": "subject-001", "trial_count": 2}]}


def verified_server(response, context):
    def respond(method, path, headers, body):
        if headers.get("Authorization") != "Bearer " + response["token"]:
            return 401, {}, b""
        if path.endswith("/identity"):
            return json_response({**{key: value for key, value in response.items() if key != "token"},
                                  "last_used_at": 2})
        return json_response(context)
    return server(respond)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "config"
        self.profiles = ConnectionProfiles(self.base)

    def pair(self, name="default"):
        return self.profiles.connect(name, "https://example.org", "pair-code", "workstation",
                                     allow_file_token=True)

    def test_publish_uses_verified_study_name_and_never_overwrites(self):
        with verified_server(device(), study_context()) as (origin, requests):
            name = self.profiles.publish(origin, device(), study_context())
            root = self.profiles.directory(name)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(name, "practice-study-bbbbbbbb")
            self.assertEqual(self.profiles.publish(origin, device(), study_context()), name)
        self.assertEqual(RunnerConfig.load(root).experiment_id, "b" * 32)
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
        self.assertEqual(RunnerConfig.load(root).read_token(root), TOKEN)
        self.assertNotIn(TOKEN, (root / "config.json").read_text())
        self.assertNotIn(TOKEN, repr(self.profiles.names()))
        self.assertFalse((self.base / "config.json").exists())
        for directory in (self.base, root.parent, root):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_publish_names_unicode_bounds_digits_and_same_name_different_studies(self):
        cases = [("Practice study", "b" * 32, "practice-study-bbbbbbbb"),
                 ("Practice study", "a" * 32, "practice-study-aaaaaaaa"),
                 ("Étude café", "b" * 32, "etude-cafe-bbbbbbbb"),
                 ("研究", "b" * 32, "study-bbbbbbbb"),
                 ("9 trials", "b" * 32, "study-9-trials-bbbbbbbb"),
                 ("A" * 120, "b" * 32, "a" * 39 + "-bbbbbbbb")]
        for title, experiment, expected in cases:
            response = {**device(), "experiment_id": experiment}
            context = study_context(title, experiment)
            with self.subTest(title=title), verified_server(response, context) as (origin, requests):
                name = self.profiles.publish(origin, response, context)
            self.assertEqual(name, expected)
            self.assertLessEqual(len(name), 48)

    def test_publish_verifies_token_and_context_before_creating_any_paths(self):
        invalid_contexts = [study_context(experiment_id="f" * 32),
                            {**study_context(), "study_name": "Spoofed name"},
                            {**study_context(), "subjects": []}, {**study_context(), "extra": TOKEN}]
        with verified_server(device(), study_context()) as (origin, requests):
            for context in invalid_contexts:
                with self.subTest(context=context), self.assertRaises(ContractError):
                    self.profiles.publish(origin, device(), context)
                self.assertFalse(self.base.exists())
        with server(lambda *args: (401, {}, TOKEN.encode())) as (origin, requests):
            with self.assertRaises(ApiError):
                self.profiles.publish(origin, device(), study_context())
        self.assertFalse(self.base.exists())

    def test_publish_snapshots_device_before_network_so_only_verified_token_is_written(self):
        response = device()

        def respond(method, path, headers, body):
            if path.endswith("/identity"):
                return json_response({**{key: value for key, value in device().items() if key != "token"},
                                      "last_used_at": 2})
            response["token"] = "unverified-token"
            return json_response(study_context())

        with server(respond) as (origin, requests):
            name = self.profiles.publish(origin, response, study_context())
        root = self.profiles.directory(name)
        self.assertEqual(RunnerConfig.load(root).read_token(root), TOKEN)

    def test_publish_rejects_symlink_components_without_writing_credentials(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)
        self.base.symlink_to(outside, target_is_directory=True)
        with verified_server(device(), study_context()) as (origin, requests):
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, device(), study_context())
        self.assertEqual(list(outside.iterdir()), [])

    def test_publish_concurrent_destination_wins_without_overwrite_or_extra_token(self):
        real_link = os.link

        def competing_link(source, target, **kwargs):
            if Path(target).name == "config.json":
                save_pairing(Path(target).parent, "https://existing.example", device(), allow_file_token=True)
            return real_link(source, target, **kwargs)

        with verified_server(device(), study_context()) as (origin, requests):
            with patch("dbp_pgl_runner.profiles.os.link", side_effect=competing_link):
                with self.assertRaises(ContractError):
                    self.profiles.publish(origin, device(), study_context())
        root = self.profiles.directory("practice-study-bbbbbbbb")
        self.assertEqual(RunnerConfig.load(root).server_origin, "https://existing.example")
        self.assertEqual(len(list(root.glob("token-*"))), 1)

    def test_publish_rejects_conflicts_and_preserves_existing_credentials(self):
        response = device()
        context = study_context()
        with verified_server(response, context) as (origin, requests):
            name = self.profiles.publish(origin, response, context)
            root = self.profiles.directory(name)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            response["device_id"] = "f" * 32
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, response, study_context())
            response["device_id"] = "d" * 32
            response["experiment_id"] = "b" * 8 + "a" * 24
            context["experiment_id"] = response["experiment_id"]
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, response, context)
        with verified_server(device(), study_context()) as (origin, requests):
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, device(), study_context())
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    def test_interrupted_publish_removes_staging_and_partial_token_then_can_retry(self):
        real_link = os.link

        def interrupted(source, target, **kwargs):
            if Path(target).name == "config.json":
                raise OSError("interrupted publication")
            return real_link(source, target, **kwargs)

        with verified_server(device(), study_context()) as (origin, requests):
            with patch("dbp_pgl_runner.profiles.os.link", side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.profiles.publish(origin, device(), study_context())
            self.assertEqual(self.profiles.names(), [])
            self.assertEqual(list(self.base.rglob("token-*")), [])
            self.assertEqual(list(self.base.rglob(".pairing-*")), [])
            self.assertEqual(self.profiles.publish(origin, device(), study_context()), "practice-study-bbbbbbbb")

    def test_publish_refuses_unsafe_or_invalid_existing_destination(self):
        root = self.base / "profiles" / "practice-study-bbbbbbbb"
        self.base.mkdir(mode=0o700)
        root.parent.mkdir(mode=0o700)
        root.mkdir(mode=0o700)
        target = root / "config.json"
        target.write_bytes(b"invalid")
        target.chmod(0o600)
        with verified_server(device(), study_context()) as (origin, requests):
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, device(), study_context())
            self.assertEqual(target.read_bytes(), b"invalid")
            target.unlink()
            before = list(root.iterdir())
            root.chmod(0o755)
            with self.assertRaises(ContractError):
                self.profiles.publish(origin, device(), study_context())
        self.assertEqual(list(root.iterdir()), before)

    def test_default_maps_legacy_base_and_lookup_does_not_create_directories(self):
        self.assertEqual(self.profiles.directory(), self.base)
        self.assertEqual(self.profiles.directory("pilot-1"), self.base / "profiles" / "pilot-1")
        self.assertEqual(self.profiles.names(), [])
        self.assertFalse(self.base.exists())

    def test_profile_slug_is_strict_and_bounded(self):
        for name in ("", ".", "..", "../outside", "/outside", "a/b", "a\\b", "Pilot", "1pilot",
                     "pilot_1", "a" * 49, "é", "pilot\n", " pilot", None, True):
            with self.subTest(name=name), self.assertRaises(ContractError):
                self.profiles.directory(name)
        self.assertEqual(self.profiles.directory("a" * 48).name, "a" * 48)

    def test_names_only_include_valid_configs_and_do_not_copy_legacy_credentials(self):
        config = save_pairing(self.base, "https://example.org", device(), allow_file_token=True)
        initial = {path.name: path.read_bytes() for path in self.base.iterdir()}
        profiles = self.base / "profiles"
        profiles.mkdir(mode=0o700)
        for name in ("zeta", "alpha", "bad", "default", "Upper"):
            directory = profiles / name
            directory.mkdir(mode=0o700)
            if name == "bad":
                (directory / "config.json").write_text("not json")
                (directory / "config.json").chmod(0o600)
            else:
                save_pairing(directory, "https://example.org", device(), allow_file_token=True)
        (profiles / "empty").mkdir(mode=0o700)
        self.assertEqual(self.profiles.names(), ["default", "alpha", "zeta"])
        self.assertEqual(RunnerConfig.load(self.base), config)
        self.assertEqual({name: (self.base / name).read_bytes() for name in initial}, initial)

    def test_pairing_creates_private_independent_profile_and_returns_no_secret(self):
        with patch("dbp_pgl_runner.profiles.RunnerApi.pair", return_value=device()) as pair:
            result = self.pair("pilot-1")
        root = self.profiles.directory("pilot-1")
        config = RunnerConfig.load(root)
        self.assertEqual(result, {"device_id": "d" * 32, "experiment_id": "b" * 32})
        self.assertEqual(config.read_token(root), TOKEN)
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(self.profiles.names(), ["pilot-1"])
        self.assertFalse((self.base / "config.json").exists())
        pair.assert_called_once_with("https://example.org", "pair-code", "workstation")
        for directory in (self.base, root.parent, root):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in root.iterdir():
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_existing_configuration_even_invalid_or_linked_is_never_overwritten(self):
        self.base.mkdir(mode=0o700)
        target = self.base / "config.json"
        for value in (b"broken", b"{}"):
            target.write_bytes(value)
            target.chmod(0o600)
            with patch("dbp_pgl_runner.profiles.RunnerApi.pair") as pair, self.assertRaises(ContractError):
                self.pair()
            pair.assert_not_called()
            self.assertEqual(target.read_bytes(), value)
        target.unlink()
        target.symlink_to(self.base / "absent")
        with patch("dbp_pgl_runner.profiles.RunnerApi.pair") as pair, self.assertRaises(ContractError):
            self.pair()
        pair.assert_not_called()
        self.assertTrue(target.is_symlink())

    def test_requires_explicit_consent_before_pairing_or_creating_paths(self):
        with patch("dbp_pgl_runner.profiles.RunnerApi.pair") as pair, self.assertRaises(ContractError):
            self.profiles.connect("pilot", "https://example.org", "pair-code", "workstation")
        pair.assert_not_called()
        self.assertFalse(self.base.exists())

    def test_symlink_and_nonprivate_profile_components_are_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)
        self.base.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.profiles.directory("pilot")
        self.base.unlink()
        self.base.mkdir(mode=0o700)
        (self.base / "profiles").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError):
            self.profiles.directory("pilot")
        (self.base / "profiles").unlink()
        (self.base / "profiles").mkdir(mode=0o700)
        (self.base / "profiles" / "pilot").mkdir(mode=0o755)
        with self.assertRaises(ContractError):
            self.profiles.directory("pilot")
        self.assertEqual(self.profiles.names(), [])

    def test_concurrent_pairing_consumes_only_one_code(self):
        entered, release = threading.Event(), threading.Event()

        def response(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test pairing timed out")
            return device()

        with patch("dbp_pgl_runner.profiles.RunnerApi.pair", side_effect=response) as pair:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.pair, "pilot")
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaises(ContractError):
                        self.pair("pilot")
                finally:
                    release.set()
                first.result()
            self.assertEqual(pair.call_count, 1)
        self.assertEqual(self.profiles.names(), ["pilot"])

    def test_configuration_appearing_during_exchange_is_preserved(self):
        def response(*args):
            save_pairing(self.base, "https://existing.example", device(), allow_file_token=True)
            return device()

        with patch("dbp_pgl_runner.profiles.RunnerApi.pair", side_effect=response), self.assertRaises(ContractError):
            self.pair()
        config = RunnerConfig.load(self.base)
        self.assertEqual(config.server_origin, "https://existing.example")
        self.assertEqual(config.read_token(self.base), TOKEN)
        self.assertEqual(len(list(self.base.glob("token-*"))), 1)

    def test_failed_exchange_does_not_publish_configuration(self):
        with patch("dbp_pgl_runner.profiles.RunnerApi.pair", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                self.pair("pilot")
        self.assertEqual(self.profiles.names(), [])
        self.assertFalse((self.profiles.directory("pilot") / "config.json").exists())

    def test_profile_replaced_by_symlink_during_exchange_receives_no_credentials(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir(mode=0o700)

        def response(*args):
            root = self.profiles.directory("pilot")
            root.rename(root.parent / "moved")
            root.symlink_to(outside, target_is_directory=True)
            return device()

        with patch("dbp_pgl_runner.profiles.RunnerApi.pair", side_effect=response), self.assertRaises(ContractError):
            self.pair("pilot")
        self.assertEqual(list(outside.iterdir()), [])
