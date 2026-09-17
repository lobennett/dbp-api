from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dbp_pgl_runner.api import ApiError
from dbp_pgl_runner.browser_pairing import BrowserPairing
from dbp_pgl_runner.config import RunnerConfig
from dbp_pgl_runner.models import ContractError
from dbp_pgl_runner.profiles import ConnectionProfiles
from tests.fixtures import TOKEN, device
from tests.http_fixture import server
from tests.test_api import json_response


def study():
    return {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
            "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Practice study",
            "subjects": [{"subject_id": "subject-001", "trial_count": 2}]}


def device_identity():
    value = device()
    del value["token"]
    return {**value, "last_used_at": 2}


class FakeClock:
    def __init__(self):
        self.now = 1000
        self.waits = []
        self.cancelled = False

    def time(self):
        return self.now

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.cancelled


class BrowserPairingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pairing = BrowserPairing()
        self.polls = []
        self.poll_states = ["pending", "approved"]
        self.expiry = 1900
        self.documents = {"exchange": device(), "identity": device_identity(), "study": study()}
        self.opened = []
        self.addCleanup(patch.stopall)
        patch("dbp_pgl_runner.browser_pairing.time.time", self.clock.time).start()
        patch("dbp_pgl_runner.browser_pairing.time.monotonic", self.clock.time).start()
        patch("dbp_pgl_runner.browser_pairing.webbrowser.open",
              side_effect=lambda url: self.opened.append(url) or True).start()

    def respond(self, method, path, headers, body):
        if path.endswith("/authorizations"):
            return json_response({"request_id": "a" * 32, "user_code": "AAAA-AAAA",
                                  "verification_path": "/runner/authorize?request=" + "a" * 32,
                                  "expires_at": self.expiry, "interval": 5}, 201)
        if path.endswith("/poll"):
            self.polls.append(self.clock.now)
            status = self.poll_states.pop(0) if self.poll_states else "pending"
            value = {"status": status, "expires_at": self.expiry}
            if status == "approved":
                value["experiment_id"] = "b" * 32
            return json_response(value)
        suffix = path.rsplit("/", 1)[1]
        return json_response(self.documents[suffix], 201 if suffix == "exchange" else 200)

    def test_random_challenge_browser_path_and_verified_result_without_verifier(self):
        with server(self.respond) as (origin, requests):
            pending = self.pairing.start(origin + "/", "lab-mac")
            result = self.pairing.wait(pending, self.clock)
            with self.assertRaises(ApiError):
                self.pairing.wait(pending, self.clock)
        self.assertEqual(result, {"device_response": device(), "study_context": study()})
        verifier = json.loads(requests[1][3])["verifier"]
        self.assertRegex(verifier, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(json.loads(requests[0][3])["challenge"], hashlib.sha256(verifier.encode()).hexdigest())
        self.assertEqual(self.opened, [origin + "/runner/authorize?request=" + "a" * 32])
        self.assertNotIn(verifier, repr(pending))
        self.assertNotIn(verifier, repr(asdict(pending)))
        self.assertNotIn(verifier, repr(result))
        self.assertEqual(pending.user_code, "AAAA-AAAA")
        self.assertGreaterEqual(self.polls[1] - self.polls[0], 5)
        self.assertEqual(sum(path.endswith("/exchange") for _, path, *_ in requests), 1)
        for _, path, headers, _ in requests:
            if path.endswith(("/study", "/identity")):
                self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

    def test_browser_approval_to_published_profile_uses_real_http_and_private_storage(self):
        with tempfile.TemporaryDirectory() as temporary, server(self.respond) as (origin, requests):
            profiles = ConnectionProfiles(Path(temporary) / "connections")
            pending = self.pairing.start(origin, "lab-mac")
            result = self.pairing.wait(pending, self.clock)
            self.assertFalse(profiles.base.exists())
            name = profiles.publish(origin, **result)
            self.assertEqual(profiles.names(), ["practice-study-bbbbbbbb"])
            root = profiles.directory(name)
            config = RunnerConfig.load(root)
            self.assertEqual(config.server_origin, origin)
            self.assertEqual(config.read_token(root), TOKEN)
            self.assertEqual(config.experiment_id, "b" * 32)
            self.assertEqual(sum(path.endswith("/exchange") for _, path, *_ in requests), 1)
            self.assertEqual(sum(path.endswith("/identity") for _, path, *_ in requests), 2)
            self.assertEqual(sum(path.endswith("/study") for _, path, *_ in requests), 2)

    def test_cancel_and_expiry_prevent_exchange(self):
        for cancelled in (True, False):
            self.clock.cancelled = cancelled
            self.expiry = self.clock.now + 10
            self.poll_states = []
            with server(self.respond) as (origin, requests):
                pending = self.pairing.start(origin, "lab-mac")
                with self.assertRaises(ApiError):
                    self.pairing.wait(pending, self.clock)
            self.assertFalse(any(path.endswith("/exchange") for _, path, *_ in requests))
            self.assertLessEqual(self.clock.now, self.expiry)

    def test_real_cancellation_wakes_wait_without_polling(self):
        with server(self.respond) as (origin, requests):
            pending = self.pairing.start(origin, "lab-mac")
            cancelled = threading.Event()
            timer = threading.Timer(0.05, cancelled.set)
            timer.start()
            try:
                with self.assertRaises(ApiError):
                    self.pairing.wait(pending, cancelled)
            finally:
                timer.join()
        self.assertEqual(len(requests), 1)

    def test_denial_and_browser_failure_never_exchange(self):
        self.poll_states = ["denied"]
        with server(self.respond) as (origin, requests):
            pending = self.pairing.start(origin, "lab-mac")
            with self.assertRaises(ApiError):
                self.pairing.wait(pending, self.clock)
        self.assertEqual(len(requests), 2)
        for failure in (False, OSError("browser failed")):
            with server(self.respond) as (origin, requests):
                with patch("dbp_pgl_runner.browser_pairing.webbrowser.open") as browser:
                    if failure is False:
                        browser.return_value = False
                    else:
                        browser.side_effect = failure
                    with self.assertRaises(ApiError):
                        self.pairing.start(origin, "lab-mac")
            self.assertEqual(len(requests), 1)

    def test_identity_study_and_approval_mismatches_never_return_credentials(self):
        for endpoint, field in [("exchange", "experiment_id"), ("identity", "experiment_id"),
                                ("identity", "device_id"), ("identity", "owner_id"),
                                ("study", "experiment_id"), ("study", "pgl_ready")]:
            original = self.documents[endpoint][field]
            self.documents[endpoint][field] = True if field == "pgl_ready" else "f" * 32
            self.poll_states = ["approved"]
            with self.subTest(endpoint=endpoint, field=field), server(self.respond) as (origin, requests):
                pending = self.pairing.start(origin, "lab-mac")
                with self.assertRaises(ContractError):
                    self.pairing.wait(pending, self.clock)
            self.documents[endpoint][field] = original

    def test_failed_exchange_is_not_retried_even_by_second_wait(self):
        self.poll_states = ["approved"]

        def respond(*args):
            if args[1].endswith("/exchange"):
                return 503, {}, TOKEN.encode()
            return self.respond(*args)

        with server(respond) as (origin, requests):
            pending = self.pairing.start(origin, "lab-mac")
            for _ in range(2):
                with self.assertRaises(ApiError) as caught:
                    self.pairing.wait(pending, self.clock)
                self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(sum(path.endswith("/exchange") for _, path, *_ in requests), 1)

    def test_concurrent_waiters_cannot_exchange_twice(self):
        self.poll_states = ["approved"]
        entered, release = threading.Event(), threading.Event()

        def respond(*args):
            if args[1].endswith("/poll"):
                entered.set()
                if not release.wait(5):
                    return 503, {}, b""
            return self.respond(*args)

        with server(respond) as (origin, requests), ThreadPoolExecutor(max_workers=1) as pool:
            pending = self.pairing.start(origin, "lab-mac")
            first = pool.submit(self.pairing.wait, pending, self.clock)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ApiError):
                    self.pairing.wait(pending, self.clock)
            finally:
                release.set()
            self.assertEqual(first.result()["study_context"], study())
        self.assertEqual(sum(path.endswith("/exchange") for _, path, *_ in requests), 1)

    def test_cancellation_during_approval_or_verification_prevents_next_request(self):
        for suffix in ("/poll", "/exchange", "/identity", "/study"):
            self.clock.cancelled = False
            self.poll_states = ["approved"]

            def respond(*args):
                if args[1].endswith(suffix):
                    self.clock.cancelled = True
                return self.respond(*args)

            with self.subTest(suffix=suffix), server(respond) as (origin, requests):
                pending = self.pairing.start(origin, "lab-mac")
                with self.assertRaises(ApiError):
                    self.pairing.wait(pending, self.clock)
            self.assertTrue(requests[-1][1].endswith(suffix))

    def test_monotonic_deadline_cannot_be_extended_by_clock_rollback(self):
        self.expiry = 1010
        self.poll_states = []
        wall_times = iter([1000, 1000, 1000, 1000, 999, 999])
        with patch("dbp_pgl_runner.browser_pairing.time.time", side_effect=lambda: next(wall_times, 999)):
            with server(self.respond) as (origin, requests):
                pending = self.pairing.start(origin, "lab-mac")
                with self.assertRaises(ApiError):
                    self.pairing.wait(pending, self.clock)
        self.assertEqual(self.clock.now, 1010)
        self.assertEqual(len(requests), 2)

    def test_expiry_changed_during_poll_is_rejected(self):
        def respond(*args):
            if args[1].endswith("/poll"):
                self.expiry += 100
            return self.respond(*args)

        with server(respond) as (origin, requests):
            pending = self.pairing.start(origin, "lab-mac")
            with self.assertRaises(ContractError):
                self.pairing.wait(pending, self.clock)
        self.assertEqual(len(requests), 2)

    def test_slow_approval_and_preexpired_start_are_rejected(self):
        self.poll_states = ["approved"]

        def respond(*args):
            if args[1].endswith("/poll"):
                self.clock.now = self.expiry
            return self.respond(*args)

        with server(respond) as (origin, requests):
            pending = self.pairing.start(origin, "lab-mac")
            with self.assertRaises(ApiError):
                self.pairing.wait(pending, self.clock)
            with self.assertRaises(ApiError):
                self.pairing.start(origin, "lab-mac")
        self.assertFalse(any(path.endswith("/exchange") for _, path, *_ in requests))
        self.assertEqual(len(self.opened), 1)
