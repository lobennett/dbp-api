from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbp_pgl_runner.cli import main
from tests.fixtures import MEDIA, TOKEN, block, device
from tests.http_fixture import server
from tests.test_api import json_response


class CliTests(unittest.TestCase):
    def test_run_requires_explicit_nonparticipant_acknowledgement(self):
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            result = main(["run", "s001"])
        self.assertNotEqual(result, 0)
        self.assertIn("--integration-test", output.getvalue())

    def test_no_command_line_secret_option(self):
        for option in ["--token", "--code", "--pairing-code"]:
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(SystemExit) as caught:
                main(["connect", option, "secret-must-not-echo"])
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("secret-must-not-echo", output.getvalue())

    def test_connect_prepare_and_offline_status_real_http(self):
        def respond(method, path, headers, body):
            if path.endswith("exchange"):
                return json_response(device(), 201)
            if path.endswith("identity"):
                identity = device()
                del identity["token"]
                identity["last_used_at"] = 2
                return json_response(identity)
            if path.endswith("/next"):
                return json_response(block())
            return 200, {"Content-Length": len(MEDIA)}, MEDIA

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = ["--config-dir", str(root / "config"), "--cache-root", str(root / "cache"),
                      "--work-root", str(root / "work")]
            output = io.StringIO()
            with server(respond) as (origin, requests):
                with (patch("builtins.input", side_effect=[origin, "yes"]),
                      patch("dbp_pgl_runner.cli.getpass.getpass", return_value="pair-secret"),
                      redirect_stdout(output), redirect_stderr(output)):
                    self.assertEqual(main(common + ["connect", "--device-name", "test workstation"]), 0)
                    self.assertEqual(main(common + ["prepare", "s001"]), 0)
            request_count = len(requests)
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(main(common + ["status", "s001"]), 0)
            self.assertEqual(len(requests), request_count)
            self.assertIn('"preparation_ready": true', output.getvalue())
            self.assertIn('"pgl_ready": false', output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertNotIn("pair-secret", output.getvalue())

    def test_noninteractive_connect_never_falls_back_to_echoed_secret(self):
        output = io.StringIO()
        with (tempfile.TemporaryDirectory() as temporary,
              patch("dbp_pgl_runner.cli.getpass.getpass", side_effect=__import__("getpass").GetPassWarning),
              redirect_stdout(output), redirect_stderr(output)):
            result = main(["--config-dir", str(Path(temporary) / "config"),
                           "connect", "--server", "https://example.org", "--allow-file-token"])
        self.assertNotEqual(result, 0)

    def test_unsafe_config_does_not_consume_one_time_pairing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "config"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            output = io.StringIO()
            with server(lambda *args: json_response(device(), 201)) as (origin, requests):
                with (patch("dbp_pgl_runner.cli.getpass.getpass", return_value="pair-secret"),
                      redirect_stdout(output), redirect_stderr(output)):
                    result = main(["--config-dir", str(root), "connect", "--server", origin,
                                   "--allow-file-token"])
                self.assertNotEqual(result, 0)
                self.assertEqual(requests, [])

    def test_status_missing_preparation_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = main(["--config-dir", str(Path(temporary) / "config"), "status", "s001"])
            self.assertNotEqual(result, 0)
            self.assertNotIn('"preparation_ready": true', output.getvalue())
            self.assertFalse((Path(temporary) / "config").exists())

    def test_unwritable_config_does_not_consume_pairing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "config"
            root.mkdir(mode=0o700)
            root.chmod(0o500)
            try:
                output = io.StringIO()
                with server(lambda *args: json_response(device(), 201)) as (origin, requests):
                    with (patch("dbp_pgl_runner.cli.getpass.getpass", return_value="pair-secret"),
                          redirect_stdout(output), redirect_stderr(output)):
                        result = main(["--config-dir", str(root), "connect", "--server", origin,
                                       "--allow-file-token"])
                    self.assertNotEqual(result, 0)
                    self.assertEqual(requests, [])
            finally:
                root.chmod(0o700)
