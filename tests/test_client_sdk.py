import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from dbp_api import (
    ApiError, Assignments, Client, ExperimentSpec, Image, Media, MetricFilter, Stimulus,
    UnsupportedMediaError, Video, media_from_row,
)
from tests.http_fixture import server


def seal(value):
    payload = {key: item for key, item in value.items() if key != "manifest_sha256"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return payload | {"manifest_sha256": digest}


def media_response(payload=b"video"):
    return 200, {"Content-Type": "video/mp4", "Content-Length": str(len(payload)),
                 "X-Content-SHA256": hashlib.sha256(payload).hexdigest()}, payload


def manifest():
    return seal({
        "schema_version": "dbp-experiment-v1", "experiment_id": "experiment-1",
        "subject_id": "subject-001", "manifest_sha256": "a" * 64,
        "blocks": [{"block_index": 1, "trials": [
            {"trial_id": "trial-1", "media_id": "video-1", "media_type": "video",
             "role": "parent", "segment": None},
            {"trial_id": "trial-2", "media_id": "video-1", "media_type": "video",
             "role": "foil", "segment": {"start_seconds": 1, "end_seconds": 2}},
        ]}],
    })


def response(value, status=200, **headers):
    return status, {"Content-Type": "application/json", **headers}, json.dumps(value).encode()


class ModelTests(unittest.TestCase):
    def test_media_descriptors_accept_real_catalog_ids_without_path_use(self):
        for media_id in ("--42OoF8WJM_part_00000011", "catalog/path:video", "café"):
            self.assertEqual(media_from_row({"clip_id": media_id}), Video(media_id))
        self.assertIsInstance(media_from_row({"media_id": "image", "media_type": "image"}), Image)
        for media_id in (" padded", "bad\n", "", "x" * 257):
            with self.subTest(media_id=media_id), self.assertRaises(ValueError):
                Video(media_id)

    def test_abstract_media_and_explicit_future_types(self):
        with self.assertRaises(TypeError):
            Media("video-1")
        self.assertEqual(Video("video-1").media_type, "video")
        for media in (Image, Stimulus):
            with self.subTest(media=media), self.assertRaises(UnsupportedMediaError):
                Client("https://example.org")._create_experiment(
                    ExperimentSpec("test", "seed", 1, 1), media_type=media)

    def test_total_parents_and_extra_foils(self):
        spec = ExperimentSpec("test", "seed", 100, 50, block_count=5, foils_per_block=4)
        self.assertEqual(spec.to_dict()["parents_per_subject"], 50)
        self.assertEqual(spec.to_dict()["foils_per_subject"], 20)
        self.assertEqual(spec.trials_per_subject, 70)
        self.assertEqual(spec.to_dict()["foils_per_block"], 4)

    def test_invalid_settings(self):
        for changes in ({"subject_count": True}, {"parents_per_subject": 0},
                        {"seed": 12}, {"block_count": 0}, {"shared_per_subject": 51},
                        {"repeats_per_subject": -1}, {"foils_per_block": -1},
                        {"block_count": 3}, {"subject_count": 101}, {"repeats_per_subject": 51},
                        {"foils_per_subject": 51}, {"block_count": 5, "foils_per_block": 11},
                        {"foils_per_block": 4, "foils_per_subject": 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ExperimentSpec(**({"name": "test", "seed": "seed", "subject_count": 1,
                                   "parents_per_subject": 50} | changes))

    def test_metric_filter_serialization_and_validation(self):
        self.assertEqual(MetricFilter("has_audio", "is", True).to_dict()["value"], True)
        self.assertIsNone(MetricFilter("duration", "gte", None).to_dict()["value"])
        self.assertEqual(MetricFilter("duration", "gte", 3).to_dict(),
                         {"metric_id": "duration", "operator": "gte", "value": 3})
        for args in (("", "gte", 1), ("duration", "???", 1),
                     ("duration", "gt", float("nan"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                MetricFilter(*args)


class ClientTests(unittest.TestCase):
    def test_subject_query_uses_saved_version_and_keeps_assignments_server_owned(self):
        def respond(method, path, headers, body):
            if path.endswith("/login"):
                return response({"csrf_token": "csrf"})
            if method == "GET":
                return response({"collection_id": "b" * 32, "version": "saved-version",
                                 "recipe": {"custom_metrics": [{"id": "custom"}]}})
            return response({"body": json.loads(body)})
        with server(respond) as (origin, requests):
            client = Client(origin)
            client.login("user", "password")
            result = client.query_media(experiment_id="a" * 32, subject_id="subject-001",
                                        filters=[MetricFilter("duration_seconds", "gte", 3)])
            self.assertEqual(result["body"]["study"], {"collection_id": "b" * 32,
                             "study_id": "a" * 32, "subject_id": "subject-001"})
            self.assertEqual(result["body"]["version"], "saved-version")
            self.assertEqual(result["body"]["custom_metrics"], [{"id": "custom"}])
            with self.assertRaises(ValueError):
                client.query_media(subject_id="subject-001")

    def test_auth_cookies_csrf_endpoints_and_request_bodies(self):
        def respond(method, path, headers, body):
            if path == "/api/auth/login":
                self.assertEqual(json.loads(body), {"username": "user", "password": "secret"})
                return response({"csrf_token": "csrf-1", "user": {"username": "user"}},
                                **{"Set-Cookie": "session=private; HttpOnly; Path=/"})
            self.assertEqual(headers.get("Cookie"), "session=private")
            if method == "POST":
                self.assertEqual(headers.get("X-CSRF-Token"), "csrf-2")
            if path == "/api/auth/session":
                return response({"csrf_token": "csrf-2", "user": {"username": "user"}})
            return response({"path": path, "body": json.loads(body) if body else None})

        with server(respond) as (origin, requests):
            client = Client(origin)
            client.login("user", "secret")
            client.session()
            self.assertEqual(client.metrics()["path"], "/api/v1/metrics")
            query = client.query_media(filters=[MetricFilter("duration", "gt", 2)],
                                       version="v1", cursor="next", limit=5,
                                       content_query="cat", corpus="both", mode="keyword",
                                       custom_metrics=[{"id": "custom"}])
            self.assertEqual(query["path"], "/api/v1/media/query")
            self.assertEqual(query["body"], {
                "filters": [{"metric_id": "duration", "operator": "gt", "value": 2}],
                "version": "v1", "cursor": "next", "limit": 5, "content_query": "cat",
                "corpus": "both", "mode": "keyword", "custom_metrics": [{"id": "custom"}],
            })
            created = client._create_experiment(ExperimentSpec("test", "seed", 1, 50,
                                                block_count=5, foils_per_block=4), version="v1")
            self.assertEqual(created["path"], "/api/v1/experiments")
            self.assertEqual(created["body"]["settings"]["foils_per_subject"], 20)
            self.assertEqual(created["body"]["media_type"], "video")
            self.assertEqual(client.experiments()["path"], "/api/v1/experiments")
            self.assertEqual(client.experiment("experiment-1")["path"],
                             "/api/v1/experiments/experiment-1")
            self.assertEqual(client.publish("experiment-1")["body"], {})
            preview = client.preview_custom_metric("cat", version="v1")
            self.assertEqual(preview["path"], "/api/metrics/custom/preview")
            self.assertEqual(preview["body"], {"query": "cat", "version": "v1",
                             "corpus": "both", "method": "keyword_bm25_v1"})
            client.logout()
            with self.assertRaises(ApiError):
                client.query_media()
            self.assertEqual(len(requests), 10)
            self.assertNotIn("secret", repr(vars(client)))

    def test_preserve_semantic_query_and_list_pagination(self):
        def respond(method, path, headers, body):
            if path.endswith("/login"):
                return response({"csrf_token": "csrf"})
            return response({"path": path, "body": json.loads(body) if body else None,
                             "next_offset": 25})
        with server(respond) as (origin, requests):
            client = Client(origin)
            client.login("user", "password")
            options = dict(content_query="cats", mode="hybrid", version="v1", search_version="search-v1",
                           relevance_min=10, relevance_max=90, cpu_pool="scored")
            for result in (client.query_media(**options), client._create_experiment(
                    ExperimentSpec("test", "seed", 1, 1), **options)):
                for key, value in options.items():
                    self.assertEqual(result["body"][key], value)
            result = client.experiments(limit=5, offset=20)
            self.assertEqual(result["path"], "/api/v1/experiments?limit=5&offset=20")
            self.assertEqual(result["next_offset"], 25)

    def test_actionable_details_only_for_expected_non_auth_errors(self):
        for status in (400, 409):
            with server(lambda *args: response({"detail": "Insufficient eligible videos"}, status)) as (origin, requests):
                with self.assertRaisesRegex(ApiError, "Insufficient eligible videos"):
                    Client(origin).metrics()
                with self.assertRaises(ApiError) as caught:
                    Client(origin).login("user", "secret")
                self.assertNotIn("Insufficient", str(caught.exception))

    def test_reject_unsafe_origins_and_timeouts(self):
        for origin in ("http://example.org", "https://user:secret@example.org", "https://example.org/x",
                       "https://example.org?x=1", "https://example.org#x", "file:///tmp/x",
                       "https://example.org\n", "http://localhost.evil"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Client(origin)
        for timeout in (0, -1, True, float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Client("https://example.org", timeout=timeout)

    def test_redirect_never_sends_password_or_cookie_to_destination(self):
        with server(lambda *args: response({})) as (other, leaked):
            for status in (301, 302, 303, 307, 308):
                with server(lambda *args: (status, {"Location": other + "/steal"}, b"secret")) as (origin, requests):
                    with self.assertRaises(ApiError) as caught:
                        Client(origin).login("user", "secret")
                    self.assertEqual(caught.exception.status, status)
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertEqual(len(requests), 1)
            self.assertEqual(leaked, [])

    def test_errors_invalid_json_and_auth_failure_clear_credentials(self):
        for payload, status in ((b"private password", 401), (b"not json", 200), (b"[]", 200),
                                (b'{"value":NaN}', 200), (b'{"value":1e999}', 200),
                                (b'{"x":1,"x":2}', 200)):
            with self.subTest(payload=payload):
                with server(lambda *args: (status, {}, payload)) as (origin, requests):
                    with self.assertRaises(ApiError) as caught:
                        Client(origin).metrics()
                    self.assertNotIn("private password", str(caught.exception))

    def test_timeout_passed_and_transport_details_withheld(self):
        client = Client("https://example.org", timeout=0.25)
        with patch.object(client._opener, "open", side_effect=URLError("secret")) as opened:
            with self.assertRaises(ApiError) as caught:
                client.metrics()
            self.assertEqual(opened.call_args.kwargs["timeout"], 0.25)
            self.assertNotIn("secret", str(caught.exception))

    def test_json_size_bound(self):
        with server(lambda *args: response({"large": "x" * 100})) as (origin, requests):
            with self.assertRaisesRegex(ApiError, "size limit"):
                Client(origin, max_json_bytes=50).metrics()

    def test_authenticated_redirect_does_not_leak_cookie(self):
        with server(lambda *args: response({})) as (other, leaked):
            def respond(method, path, headers, body):
                if path.endswith("/login"):
                    return response({"csrf_token": "csrf"}, **{"Set-Cookie": "session=private; Path=/"})
                return 307, {"Location": other + "/steal"}, b""
            with server(respond) as (origin, requests):
                client = Client(origin)
                client.login("user", "secret")
                with self.assertRaises(ApiError):
                    client.query_media()
                self.assertEqual(len(requests), 2)
                self.assertEqual(leaked, [])

    def test_failed_logout_and_expired_session_clear_cookie(self):
        for failure in ("logout", "session"):
            def respond(method, path, headers, body):
                if path.endswith("/login"):
                    return response({"csrf_token": "csrf"}, **{"Set-Cookie": "session=private; Path=/"})
                if path.endswith("/" + failure):
                    return response({}, 401 if failure == "session" else 500)
                self.assertIsNone(headers.get("Cookie"))
                return response({})
            with self.subTest(failure=failure), server(respond) as (origin, requests):
                client = Client(origin)
                client.login("user", "secret")
                with self.assertRaises(ApiError):
                    getattr(client, failure)()
                client.metrics()
                with self.assertRaises(ApiError):
                    client.query_media()

    def test_invalid_ids_never_make_requests(self):
        with server(lambda *args: response({})) as (origin, requests):
            client = Client(origin)
            for identifier in ("../escape", "a/b", "a%2fb", "", "a?secret", "a\\b"):
                with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                    client.experiment(identifier)
            self.assertEqual(requests, [])

    def test_create_requires_pinned_version(self):
        with server(lambda *args: response({})) as (origin, requests):
            with self.assertRaises(ValueError):
                Client(origin)._create_experiment(ExperimentSpec("test", "seed", 1, 1))
            self.assertEqual(requests, [])


class DownloadTests(unittest.TestCase):
    def test_complementary_parent_and_foil_segments_download_separately(self):
        original = manifest()
        original["blocks"][0]["trials"][0]["segment"] = dict(start_seconds=0, end_seconds=1)
        original = seal(original)

        def respond(method, path, headers, body):
            if path.endswith("/manifest"):
                return response(original)
            return media_response(b"first-half" if "/trial-1/" in path else b"second-half")

        with tempfile.TemporaryDirectory() as temporary, server(respond) as (origin, requests):
            assignments = Assignments(Client(origin), "experiment-1", ("subject-001",))
            session = assignments.subject("subject-001", workspace=Path(temporary) / "journal")
            destination = Path(temporary) / "videos" / "experiment-1" / "subject-001"
            trials = session.download(destination)
            self.assertEqual(len(trials), 2)
            self.assertEqual(trials[0].role, "parent")
            self.assertEqual(trials[0].segment.start_seconds, 0)
            self.assertEqual(trials[0].segment.end_seconds, trials[1].segment.start_seconds)
            self.assertEqual(trials[0].local_path.read_bytes(), b"first-half")
            self.assertEqual(trials[1].local_path.read_bytes(), b"second-half")
            self.assertNotEqual(trials[0].local_path, trials[1].local_path)
            self.assertTrue(all(method == "GET" for method, *_ in requests))
            with self.assertRaises(FileExistsError):
                session.download(destination)
            self.assertEqual(trials[0].local_path.read_bytes(), b"first-half")

    def test_nested_download_failure_keeps_parent_contents(self):
        def respond(method, path, headers, body):
            return response(manifest()) if path.endswith("/manifest") else (500, {}, b"failed")

        with tempfile.TemporaryDirectory() as temporary, server(respond) as (origin, requests):
            parent = Path(temporary) / "downloads"
            parent.mkdir()
            retained = parent / "keep.txt"
            retained.write_text("keep")
            destination = parent / "experiment-1" / "subject-001"
            with self.assertRaises(ApiError):
                Client(origin).download_subject("experiment-1", "subject-001", destination)
            self.assertFalse(destination.exists())
            self.assertEqual(retained.read_text(), "keep")

    def test_symlink_destination_is_never_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "link"
            destination.symlink_to(Path(temporary) / "missing")
            with self.assertRaises(FileExistsError):
                Client("https://example.org").download_subject("experiment-1", "subject-001", destination)
            self.assertTrue(destination.is_symlink())

    def test_download_preserves_manifest_hash_and_uses_trial_endpoint(self):
        original = manifest()
        original["blocks"][0]["trials"][0]["media_id"] = "--42OoF8WJM_part_00000011"
        original = seal(original)
        def respond(method, path, headers, body):
            if path.endswith("/manifest"):
                return response(original)
            return media_response()
        with tempfile.TemporaryDirectory() as temporary, server(respond) as (origin, requests):
            destination = Path(temporary) / "subject"
            result = Client(origin).download_subject("experiment-1", "subject-001", destination)
            local = json.loads(result.read_text())
            self.assertEqual(local["manifest"], original)
            self.assertEqual(set(local["files"]), {"trial-1", "trial-2"})
            for path in local["files"].values():
                self.assertEqual((destination / path).read_bytes(), b"video")
            self.assertEqual(requests[-1][1],
                             "/api/v1/experiments/experiment-1/subjects/subject-001/trials/trial-2/media")
            with self.assertRaises(FileExistsError):
                Client(origin).download_subject("experiment-1", "subject-001", destination)
            self.assertEqual(json.loads(result.read_text())["manifest"], original)

    def test_partial_download_cleanup_and_size_bound(self):
        for failure in ("short", "http", "oversize", "html", "hash", "missing_hash", "missing_length"):
            def respond(method, path, headers, body):
                if path.endswith("/manifest"):
                    return response(manifest())
                if "trial-1" in path:
                    return media_response()
                _, headers, payload = media_response()
                return {"short": (200, headers | {"Content-Length": "10"}, b"bad"),
                        "http": (500, {}, b"private error"),
                        "oversize": media_response(b"x" * 11),
                        "html": (200, headers | {"Content-Type": "text/html"}, b"login"),
                        "hash": (200, headers, b"WRONG"),
                        "missing_hash": (200, {key: value for key, value in headers.items() if key != "X-Content-SHA256"}, payload),
                        "missing_length": (200, {key: value for key, value in headers.items() if key != "Content-Length"}, payload)}[failure]
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                with server(respond) as (origin, requests):
                    destination = Path(temporary) / "subject"
                    with self.assertRaises(ApiError):
                        Client(origin, max_media_bytes=10).download_subject(
                            "experiment-1", "subject-001", destination)
                    self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_bad_manifest_rejected_before_any_media_download(self):
        variants = []
        for field, value in (("trial_id", "../escape"), ("media_type", "image"),
                             ("role", "unknown"), ("segment", {"start_seconds": 2, "end_seconds": 1})):
            value_manifest = manifest()
            value_manifest["blocks"][0]["trials"][0][field] = value
            variants.append(seal(value_manifest))
        for role, segment in (("foil", None), ("parent", {"start_seconds": -1, "end_seconds": 1}),
                              ("parent", {"start_seconds": True, "end_seconds": 1}),
                              ("parent", {"start_seconds": 1, "end_seconds": 1}),
                              ("repeat", {"start_seconds": 0, "end_seconds": 1})):
            invalid = manifest()
            invalid["blocks"][0]["trials"][0].update(role=role, segment=segment)
            variants.append(seal(invalid))
        duplicate = manifest()
        duplicate["blocks"][0]["trials"].append(copy.deepcopy(duplicate["blocks"][0]["trials"][0]))
        variants.extend([seal(duplicate), seal(manifest() | {"subject_id": "other"}),
                         manifest() | {"manifest_sha256": "bad"},
                         manifest() | {"manifest_sha256": "b" * 64}])
        for value in variants:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                with server(lambda *args: response(value)) as (origin, requests):
                    with self.assertRaises((ValueError, ApiError)):
                        Client(origin).download_subject("experiment-1", "subject-001", Path(temporary) / "subject")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(list(Path(temporary).iterdir()), [])
