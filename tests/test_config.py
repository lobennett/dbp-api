import json
from pathlib import Path
import tempfile
import unittest

from dbp_pgl_runner.config import RunnerConfig, save_pairing, validate_origin
from dbp_pgl_runner.models import ContractError
from tests.fixtures import TOKEN, device


class ConfigTests(unittest.TestCase):
    def test_https_or_literal_loopback_only_without_userinfo_or_paths(self):
        for origin in ["https://example.org", "http://localhost:8000", "http://127.0.0.1:12",
                       "http://[::1]:8000"]:
            self.assertEqual(validate_origin(origin), origin)
        for origin in ["http://example.org", "http://localhost.evil", "http://127.1",
                       "http://2130706433", "https://user:password@example.org",
                       "https://example.org/path", "https://example.org?token=secret",
                       "https://example.org#fragment", "https://example.org:bad",
                       "https://example.org\n", "file:///tmp/foo", "//example.org"]:
            with self.subTest(origin=origin), self.assertRaises(ContractError):
                validate_origin(origin)

    def test_token_separate_private_and_configuration_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "config"
            config = save_pairing(root, "https://example.org", device(), allow_file_token=True)
            self.assertEqual(RunnerConfig.load(root), config)
            self.assertEqual(config.read_token(root), TOKEN)
            self.assertNotIn(TOKEN, (root / "config.json").read_text())
            self.assertNotIn(TOKEN, repr(config))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for path in root.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_file_secret_fallback_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ContractError):
                save_pairing(Path(temporary) / "config", "https://example.org", device())

    def test_rejects_insecure_permissions_symlinks_and_token_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "config"
            config = save_pairing(root, "https://example.org", device(), allow_file_token=True)
            token = root / config.token_ref
            token.chmod(0o644)
            with self.assertRaises(ContractError):
                config.read_token(root)
            token.chmod(0o600)
            token.unlink()
            token.symlink_to(Path(temporary) / "outside")
            with self.assertRaises(ContractError):
                config.read_token(root)
            source = json.loads((root / "config.json").read_text())
            source["token_ref"] = "../outside"
            (root / "config.json").write_text(json.dumps(source))
            with self.assertRaises(ContractError):
                RunnerConfig.load(root)
