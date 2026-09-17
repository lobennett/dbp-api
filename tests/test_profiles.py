from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dbp_pgl_runner.config import RunnerConfig, save_pairing
from dbp_pgl_runner.models import ContractError
from dbp_pgl_runner.profiles import ConnectionProfiles
from tests.fixtures import TOKEN, device


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "config"
        self.profiles = ConnectionProfiles(self.base)

    def pair(self, name="default"):
        return self.profiles.connect(name, "https://example.org", "pair-code", "workstation",
                                     allow_file_token=True)

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
