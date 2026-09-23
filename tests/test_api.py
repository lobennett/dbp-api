import json
from copy import deepcopy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import socket
import unittest
from unittest.mock import patch

from dbp_pgl_runner.api import ApiError, RangeNotSupported, RunnerApi
from dbp_pgl_runner import api as api_module
from dbp_pgl_runner.compatibility import expected_compatibility
from dbp_pgl_runner.config import RunnerConfig
from dbp_pgl_runner.models import BlockPackage, ContractError
from tests.fixtures import MEDIA, TOKEN, block, device, seal
from tests.http_fixture import server


def config(origin):
    return RunnerConfig(origin, "d" * 32, "b" * 32, "token-" + "f" * 32)


def json_response(value, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(value).encode()


@contextmanager
def trickle_server(responder, suffix, phase="body"):
    entered, stopped, disconnected = threading.Event(), threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.handle_response()

        def do_GET(self):
            self.handle_response()

        def handle_response(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path, dict(self.headers), body))
            status, headers, payload = responder(self.command, self.path, self.headers, body)
            slow = self.path.endswith(suffix)
            try:
                if slow and phase == "headers":
                    self.connection.sendall(f"HTTP/1.1 {status} OK\r\nX-Trickle: ".encode())
                else:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, str(value))
                    self.send_header("Content-Length", str(len(payload) + (40 if slow else 0)))
                    self.end_headers()
                if slow:
                    entered.set()
                    for _ in range(40):
                        if stopped.wait(0.03):
                            return
                        self.connection.sendall(b"x" if phase == "headers" else b" ")
                    if phase == "headers":
                        self.connection.sendall(b"\r\nConnection: close\r\n\r\n")
                self.connection.sendall(payload)
            except OSError:
                disconnected.set()

        def log_message(self, *args):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}", requests, entered, disconnected
    finally:
        stopped.set()
        instance.shutdown()
        instance.server_close()
        thread.join()


class ApiTests(unittest.TestCase):
    def test_cancelled_connection_cannot_send_a_late_request(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        request_seen = threading.Event()
        connect = socket.create_connection

        def delayed_connect(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise OSError("test connection was not released")
            try:
                return connect(*args, **kwargs)
            finally:
                finished.set()

        cancel = threading.Event()
        def respond(*args):
            request_seen.set()
            return json_response({})

        with server(respond) as (origin, requests):
            with patch("socket.create_connection", side_effect=delayed_connect):
                budget = api_module.RequestBudget(time.monotonic() + 0.15, cancel)
                try:
                    with self.assertRaises(ApiError):
                        RunnerApi.exchange_authorization(origin, "a" * 32, "v" * 43, budget=budget)
                    self.assertTrue(entered.is_set())
                finally:
                    release.set()
                self.assertTrue(finished.wait(1))
                self.assertFalse(request_seen.wait(0.2))
            self.assertEqual(requests, [])

    def test_bounded_transport_preserves_redirect_size_and_localhost_rules(self):
        with server(lambda *args: json_response({})) as (other, stolen):
            for status, headers, body in [(307, {"Location": other}, b""),
                                          (200, {"Content-Length": 33 * 1024 * 1024}, b"{}")]:
                with server(lambda *args: (status, headers, body)) as (origin, requests):
                    with self.assertRaises(ApiError):
                        RunnerApi.poll_authorization(origin, "a" * 32, "v" * 43,
                                                     budget=api_module.RequestBudget(time.monotonic() + 2,
                                                                                     threading.Event()))
                    self.assertEqual(len(requests), 1)
            self.assertEqual(stolen, [])
        with server(lambda *args: json_response({"status": "pending", "expires_at": 2000})) as (origin, requests):
            result = RunnerApi.poll_authorization(origin.replace("127.0.0.1", "localhost"),
                                                  "a" * 32, "v" * 43,
                                                  budget=api_module.RequestBudget(time.monotonic() + 2,
                                                                                  threading.Event()))
            self.assertEqual(result["status"], "pending")
            for invalid in ("http://example.org", "http://localhost.evil", "http://127.0.0.1.evil"):
                with self.assertRaises(ContractError):
                    RunnerApi.poll_authorization(invalid, "a" * 32, "v" * 43,
                                                 budget=api_module.RequestBudget(time.monotonic() + 2,
                                                                                 threading.Event()))
            self.assertEqual(len(requests), 1)

    def test_request_budget_interrupts_trickling_headers_and_body_and_closes_socket(self):
        for phase in ("headers", "body"):
            for cancellation in (True, False):
                cancel = threading.Event()
                with self.subTest(phase=phase, cancellation=cancellation):
                    with trickle_server(lambda *args: json_response({"status": "pending", "expires_at": 2000}),
                                        "/poll", phase) as (origin, requests, entered, disconnected):
                        timer = threading.Timer(0.15, cancel.set)
                        if cancellation:
                            timer.start()
                        started = time.monotonic()
                        before = set(threading.enumerate())
                        budget = api_module.RequestBudget(started + (5 if cancellation else 0.15), cancel)
                        try:
                            with self.assertRaises(ApiError):
                                RunnerApi.poll_authorization(origin, "a" * 32, "v" * 43, budget=budget)
                            self.assertLess(time.monotonic() - started, 0.7)
                            self.assertTrue(entered.is_set())
                            self.assertTrue(disconnected.wait(0.7))
                            for worker in set(threading.enumerate()) - before:
                                if worker.name == "runner-authorization-http":
                                    worker.join(0.7)
                                    self.assertFalse(worker.is_alive())
                        finally:
                            if cancellation:
                                timer.join()
                        self.assertEqual(len(requests), 1)

    def test_browser_authorization_contract_and_single_exchange(self):
        request_id = "a" * 32
        pending = {"request_id": request_id, "user_code": "AAAA-AAAA",
                   "verification_path": "/runner/authorize?request=" + request_id,
                   "expires_at": 2000, "interval": 5}
        responses = [json_response(pending, 201),
                     json_response({"status": "approved", "expires_at": 2000,
                                    "experiment_id": "b" * 32}), json_response(device(), 201)]
        with server(lambda *args: responses.pop(0)) as (origin, requests):
            self.assertEqual(RunnerApi.start_authorization(origin, "lab-mac", "f" * 64), pending)
            self.assertEqual(RunnerApi.poll_authorization(origin, request_id, "v" * 43)["status"], "approved")
            self.assertEqual(RunnerApi.exchange_authorization(origin, request_id, "v" * 43), device())
        self.assertEqual([entry[:2] for entry in requests], [
            ("POST", "/api/runner-device/authorizations"),
            ("POST", "/api/runner-device/authorizations/" + request_id + "/poll"),
            ("POST", "/api/runner-device/authorizations/" + request_id + "/exchange")])
        self.assertEqual(json.loads(requests[0][3]), {"device_name": "lab-mac", "challenge": "f" * 64})
        for entry in requests:
            self.assertNotIn("Authorization", entry[2])
            self.assertNotIn("v" * 43, entry[1])
        self.assertEqual(json.loads(requests[1][3]), {"verifier": "v" * 43})
        self.assertEqual(json.loads(requests[2][3]), {"verifier": "v" * 43})

    def test_authorization_rejects_invalid_start_and_poll_fields(self):
        pending = {"request_id": "a" * 32, "user_code": "AAAA-AAAA",
                   "verification_path": "/runner/authorize?request=" + "a" * 32,
                   "expires_at": 2000, "interval": 5}
        invalid = [None, [], {}, {**pending, "token": TOKEN}, {**pending, "status": "pending"}]
        for field, values in {
            "request_id": ["x", "A" * 32, None],
            "user_code": ["BBBB-BBBB", "aaaa-aaaa", "AAAA-AAAA\n", None],
            "verification_path": ["https://evil.test/", "//evil.test/", "/other", "\\evil.test",
                                  pending["verification_path"] + "&token=secret", "/runner/authorize?request=" + "b" * 32],
            "expires_at": [0, -1, True, float("inf"), 10 ** 400, "2000"],
            "interval": [0, -1, True, 1.5, 901, "5"],
        }.items():
            invalid.extend({**pending, field: value} for value in values)
        for value in invalid:
            with self.subTest(value=value), patch("dbp_pgl_runner.api._json", return_value=value):
                with self.assertRaises(ContractError):
                    RunnerApi.start_authorization("https://example.org", "lab-mac", "f" * 64)
        pending_poll = {"status": "pending", "expires_at": 2000}
        invalid = [None, {}, {**pending_poll, "token": TOKEN}, {**pending_poll, "extra": 1},
                   {**pending_poll, "experiment_id": "b" * 32}, {**pending_poll, "status": "approved"},
                   {**pending_poll, "status": "unknown"}, {**pending_poll, "expires_at": True},
                   {**pending_poll, "status": "approved", "experiment_id": "B" * 32}]
        for value in invalid:
            with self.subTest(value=value), patch("dbp_pgl_runner.api._json", return_value=value):
                with self.assertRaises(ContractError):
                    RunnerApi.poll_authorization("https://example.org", "a" * 32, "v" * 43)

    def test_authorization_rejects_bad_inputs_before_network(self):
        with server(lambda *args: json_response({})) as (origin, requests):
            for challenge in ("x", "F" * 64, None):
                with self.assertRaises(ContractError):
                    RunnerApi.start_authorization(origin, "lab", challenge)
            for operation in (RunnerApi.poll_authorization, RunnerApi.exchange_authorization):
                for request_id, verifier in [("../bad", "v" * 43), ("a" * 32, "v" * 42),
                                             ("a" * 32, "v" * 129), ("a" * 32, "v" * 43 + "=")]:
                    with self.assertRaises(ContractError):
                        operation(origin, request_id, verifier)
        self.assertEqual(requests, [])

    def test_authorization_redirects_oversize_and_error_bodies_are_refused_without_retries(self):
        with server(lambda *args: json_response({})) as (other, stolen):
            for status, headers, payload in [(307, {"Location": other + "/stolen"}, b""),
                                              (200, {"Content-Length": 33 * 1024 * 1024}, b"{}"),
                                              (429, {"Retry-After": "5"}, TOKEN.encode())]:
                with server(lambda *args: (status, headers, payload)) as (origin, requests):
                    with self.assertRaises(ApiError) as caught:
                        RunnerApi.poll_authorization(origin, "a" * 32, "v" * 43)
                    self.assertNotIn(TOKEN, str(caught.exception))
                    self.assertNotIn("v" * 43, str(caught.exception))
                    self.assertEqual(len(requests), 1)
            self.assertEqual(stolen, [])

    def test_study_returns_bound_canonical_roster_in_server_order(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
                    "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Pilot study",
                    "compatibility": expected_compatibility(),
                    "subjects": [{"subject_id": "subject-100", "trial_count": 50000},
                                 {"subject_id": "subject-001", "trial_count": 1}]}
        with server(lambda *args: json_response(document)) as (origin, requests):
            self.assertEqual(RunnerApi(config(origin), TOKEN).study(), document)
        self.assertEqual(requests[0][0:2], ("GET", "/api/runner-device/study"))
        self.assertEqual(requests[0][2]["Authorization"], "Bearer " + TOKEN)

    def test_study_rejects_malformed_unbound_or_ambiguous_documents(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
                    "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Pilot study",
                    "compatibility": expected_compatibility(),
                    "subjects": [{"subject_id": "subject-001", "trial_count": 1}]}
        invalid = [[], None, {}, {**document, "extra": True}]
        for field, values in {
            "schema_version": ["future", None],
            "mode": ["production", None],
            "pgl_ready": [True, 0, None],
            "experiment_id": ["a" * 32, None, 1],
            "study_id": ["bad", "C" * 32, None, 1],
            "study_name": ["", " Pilot", "Pilot ", "Pilot\n", "x" * 121, None],
            "subjects": [[], {}, None, document["subjects"] * 2, document["subjects"] * 101],
        }.items():
            for value in values:
                invalid.append({**document, field: value})
        for subject in [None, {}, {"subject_id": "subject-001", "trial_count": 1, "extra": 2}]:
            invalid.append({**document, "subjects": [subject]})
        for field, values in {
            "subject_id": ["s001", "subject-000", "subject-101", "subject-1", None, 1],
            "trial_count": [0, -1, 50001, True, 1.0, "1", None],
        }.items():
            for value in values:
                changed = deepcopy(document)
                changed["subjects"][0][field] = value
                invalid.append(changed)
        api = RunnerApi(config("https://example.org"), TOKEN)
        for value in invalid:
            with self.subTest(value=value), patch("dbp_pgl_runner.api._json", return_value=value):
                with self.assertRaises(ContractError):
                    api.study()

    def test_study_rejects_mismatched_compatibility(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test",
                    "pgl_ready": False, "experiment_id": "b" * 32, "study_id": "c" * 32,
                    "study_name": "Pilot study", "compatibility": expected_compatibility(),
                    "subjects": [{"subject_id": "subject-001", "trial_count": 1}]}
        for mutation in ("missing", "additional", "mismatch", "malformed"):
            bad = deepcopy(document)
            if mutation == "missing":
                del bad["compatibility"]
            elif mutation == "additional":
                bad["compatibility"]["extra"] = True
            elif mutation == "mismatch":
                bad["compatibility"]["pgl_integration_revision"] = "wrong"
            else:
                bad["compatibility"] = []
            with self.subTest(mutation=mutation), patch("dbp_pgl_runner.api._json", return_value=bad):
                with self.assertRaises(ContractError):
                    RunnerApi(config("https://example.org"), TOKEN).study()

    def test_attempt_endpoints_are_bound_and_uploads_are_bounded(self):
        attempt_id = "e" * 32
        package = BlockPackage.from_dict(block())
        response = {"attempt_id": attempt_id, "package_id": package.package_id,
                    "experiment_id": package.experiment_id, "device_id": "d" * 32,
                    "subject_id": package.subject_id, "package_sha256": package.package_sha256}
        with server(lambda *args: json_response(response, 201)) as (origin, requests):
            api = RunnerApi(config(origin), TOKEN)
            self.assertEqual(api.claim_attempt(package, attempt_id)["attempt_id"], attempt_id)
            self.assertEqual(json.loads(requests[0][3]), {"attempt_id": attempt_id})
            response["device_id"] = "f" * 32
            with self.assertRaises(ContractError):
                api.claim_attempt(package, attempt_id)
        with server(lambda *args: json_response({})) as (origin, requests):
            api = RunnerApi(config(origin), TOKEN)
            entry = {"artifact_id": "c" * 32, "path": "native/data.json", "bytes": 3,
                     "sha256": "f" * 64}
            api.upload_chunk(attempt_id, entry, 0, b"abc")
            body = json.loads(requests[0][3])
            self.assertEqual(body["data_base64"], "YWJj")
            self.assertEqual(body["chunk_index"], 0)
            with self.assertRaises(ContractError):
                api.upload_chunk(attempt_id, entry, 1, b"x" * (1024 * 1024 + 1))

    def test_huge_device_timestamps_raise_contract_error(self):
        for field in ("created_at", "last_used_at"):
            response = device()
            del response["token"]
            response["last_used_at"] = 2
            response[field] = 10 ** 400
            with server(lambda *args: json_response(response)) as (origin, requests):
                with self.subTest(field=field), self.assertRaises(ContractError):
                    RunnerApi(config(origin), TOKEN).identity()
        response = device()
        response["created_at"] = 10 ** 400
        with server(lambda *args: json_response(response, 201)) as (origin, requests):
            with self.assertRaises(ContractError):
                RunnerApi.pair(origin, "pair-secret", "test workstation")

    def test_pair_body_and_next_identity_and_media_exact_route(self):
        def respond(method, path, headers, body):
            if path.endswith("exchange"):
                return json_response(device(), 201)
            if path.endswith("/next"):
                return json_response(block())
            return 200, {"Content-Length": len(MEDIA)}, MEDIA

        with server(respond) as (origin, requests):
            self.assertEqual(RunnerApi.pair(origin, "pair-secret", "test workstation"), device())
            api = RunnerApi(config(origin), TOKEN)
            package = api.next_block("s001")
            self.assertEqual(b"".join(api.iter_media(package, package.trials[0])), MEDIA)
        self.assertNotIn("Authorization", requests[0][2])
        self.assertEqual(json.loads(requests[0][3]),
                         {"code": "pair-secret", "device_name": "test workstation"})
        self.assertEqual(requests[1][1], "/api/runner-device/subjects/s001/next")
        self.assertEqual(requests[2][1], "/api/runner-device/blocks/" + "a" * 32 + "/trials/0/media")
        self.assertEqual(requests[1][2]["Authorization"], "Bearer " + TOKEN)

    def test_redirect_never_leaks_bearer_or_pairing_secret(self):
        with server(lambda *args: json_response({})) as (other, stolen):
            with server(lambda *args: (307, {"Location": other + "/stolen"}, b"")) as (origin, requests):
                api = RunnerApi(config(origin), TOKEN)
                with self.assertRaises(ApiError):
                    api.next_block("s001")
                with self.assertRaises(ApiError):
                    RunnerApi.pair(origin, "pair-secret", "test workstation")
                self.assertEqual(len(requests), 2)
            self.assertEqual(stolen, [])

    def test_bound_subject_and_experiment_reject_server_identity_swap(self):
        for field, replacement in [("experiment_id", "f" * 32), ("subject_id", "subject-002")]:
            bad = block()
            bad[field] = replacement
            with server(lambda *args: json_response(seal(bad))) as (origin, requests):
                with self.assertRaises(ContractError):
                    RunnerApi(config(origin), TOKEN).next_block("s001")

    def test_identity_endpoint_matches_pairing(self):
        response = device()
        del response["token"]
        response["last_used_at"] = 2
        with server(lambda *args: json_response(response)) as (origin, requests):
            self.assertEqual(RunnerApi(config(origin), TOKEN).identity()["device_id"], "d" * 32)
            response["device_id"] = "f" * 32
            with self.assertRaises(ContractError):
                RunnerApi(config(origin), TOKEN).identity()

    def test_errors_do_not_echo_response_secrets_and_do_not_retry_writes(self):
        with server(lambda *args: (401, {}, TOKEN.encode())) as (origin, requests):
            for operation in [lambda: RunnerApi(config(origin), TOKEN).next_block("s001"),
                              lambda: RunnerApi.pair(origin, "pair-secret", "test workstation")]:
                with self.assertRaises(ApiError) as caught:
                    operation()
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("pair-secret", str(caught.exception))
            self.assertEqual(len(requests), 2)

    def test_duplicate_json_fields_and_oversized_json_are_rejected(self):
        for headers, payload in [({}, b'{"x":1,"x":2}'),
                                 ({"Content-Length": 33 * 1024 * 1024}, b"{}")]:
            with server(lambda *args: (200, headers, payload)) as (origin, requests):
                with self.assertRaises((ApiError, ContractError)):
                    RunnerApi(config(origin), TOKEN).next_block("s001")

    def test_media_range_is_exact_and_ignored_range_requires_full_restart(self):
        package = BlockPackage.from_dict(block())
        trial = package.trials[0]
        for code, content_range, error in [(206, f"bytes 4-{len(MEDIA)-1}/{len(MEDIA)}", None),
                                           (200, None, RangeNotSupported),
                                           (206, "bytes 0-1/2", ApiError)]:
            def respond(*args):
                headers = {"Content-Length": len(MEDIA) - 4}
                if content_range:
                    headers["Content-Range"] = content_range
                return code, headers, MEDIA[4:]
            with server(respond) as (origin, requests):
                operation = lambda: b"".join(RunnerApi(config(origin), TOKEN).iter_media(package, trial, offset=4))
                if error:
                    with self.assertRaises(error):
                        operation()
                else:
                    self.assertEqual(operation(), MEDIA[4:])
                self.assertEqual(requests[0][2]["Range"], "bytes=4-")

    def test_media_overflow_and_partial_response_fail_closed(self):
        package = BlockPackage.from_dict(block())
        for payload in [MEDIA + b"extra", MEDIA[:-1]]:
            with server(lambda *args: (200, {}, payload)) as (origin, requests):
                with self.assertRaises(ApiError):
                    b"".join(RunnerApi(config(origin), TOKEN).iter_media(package, package.trials[0]))
