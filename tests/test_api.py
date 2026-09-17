import json
from copy import deepcopy
import unittest
from unittest.mock import patch

from dbp_pgl_runner.api import ApiError, RangeNotSupported, RunnerApi
from dbp_pgl_runner.config import RunnerConfig
from dbp_pgl_runner.models import BlockPackage, ContractError
from tests.fixtures import MEDIA, TOKEN, block, device, seal
from tests.http_fixture import server


def config(origin):
    return RunnerConfig(origin, "d" * 32, "b" * 32, "token-" + "f" * 32)


def json_response(value, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(value).encode()


class ApiTests(unittest.TestCase):
    def test_study_returns_bound_canonical_roster_in_server_order(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
                    "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Pilot study",
                    "subjects": [{"subject_id": "subject-100", "trial_count": 50000},
                                 {"subject_id": "subject-001", "trial_count": 1}]}
        with server(lambda *args: json_response(document)) as (origin, requests):
            self.assertEqual(RunnerApi(config(origin), TOKEN).study(), document)
        self.assertEqual(requests[0][0:2], ("GET", "/api/runner-device/study"))
        self.assertEqual(requests[0][2]["Authorization"], "Bearer " + TOKEN)

    def test_study_rejects_malformed_unbound_or_ambiguous_documents(self):
        document = {"schema_version": "dbp-pgl-study-v1", "mode": "integration_test", "pgl_ready": False,
                    "experiment_id": "b" * 32, "study_id": "c" * 32, "study_name": "Pilot study",
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
