"""Bounded same-origin requests; redirects and implicit retries are forbidden."""

from contextlib import contextmanager
import base64
import hashlib
import http.client
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import validate_origin, validate_token
from .compatibility import validate_compatibility
from .models import (BlockPackage, ContractError, MAX_JSON_BYTES, canonical_subject,
                     canonical_bytes, identity, is_finite_number, normalized, strict_json)


class ApiError(RuntimeError):
    pass


class RangeNotSupported(ApiError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


@contextmanager
def _request(origin, path, *, token=None, body=None, headers=None):
    origin = validate_origin(origin)
    request_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    if token is not None:
        request_headers["Authorization"] = "Bearer " + validate_token(token)
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(origin + path, data=body, headers=request_headers)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ApiError("Encoded response refused")
            yield response
    except HTTPError as error:
        status = error.code
        error.close()
        raise ApiError(f"Runner server returned HTTP {status}; response body withheld") from None
    except (OSError, URLError, http.client.HTTPException, ValueError):
        raise ApiError("Runner request failed; details withheld to protect credentials") from None


def _json(origin, path, token=None, body=None, expected_status=200):
    with _request(origin, path, token=token, body=body) as response:
        if response.status != expected_status:
            raise ApiError("Unexpected response status")
        size = response.headers.get("Content-Length")
        if size is not None and (not size.isdecimal() or int(size) > MAX_JSON_BYTES):
            raise ApiError("JSON exceeds size limit")
        raw = response.read(MAX_JSON_BYTES + 1)
    return strict_json(raw)


def _device(value, pairing=False):
    fields = {"device_id", "experiment_id", "owner_id", "device_name", "created_at",
              "token" if pairing else "last_used_at"}
    if type(value) is not dict or set(value) != fields:
        raise ContractError("Invalid device response fields")
    for field in ("device_id", "experiment_id", "owner_id"):
        identity(value[field])
    normalized(value["device_name"], 160)
    for field in ("created_at",) if pairing else ("created_at", "last_used_at"):
        if not is_finite_number(value[field]) or value[field] < 0:
            raise ContractError("Invalid device timestamp")
    if pairing:
        validate_token(value["token"])
    return value


class RunnerApi:
    def __init__(self, config, token):
        self.config = config
        self._token = validate_token(token)

    @staticmethod
    def pair(origin, code, device_name):
        validate_token(code)
        normalized(device_name, 160)
        body = json.dumps({"code": code, "device_name": device_name}).encode()
        return _device(_json(origin, "/api/runner-device/pairings/exchange", body=body,
                             expected_status=201), pairing=True)

    def identity(self):
        result = _device(_json(self.config.server_origin, "/api/runner-device/identity", self._token))
        if (result["device_id"] != self.config.device_id
                or result["experiment_id"] != self.config.experiment_id):
            raise ContractError("Device response does not match configured identity")
        return result

    def study(self):
        result = _json(self.config.server_origin, "/api/runner-device/study", self._token)
        fields = {"schema_version", "experiment_id", "study_id", "study_name",
                  "mode", "pgl_ready", "compatibility", "subjects"}
        if type(result) is not dict or set(result) != fields:
            raise ContractError("Invalid study context fields")
        if (result["schema_version"] != "dbp-pgl-study-v1"
                or result["mode"] != "integration_test" or result["pgl_ready"] is not False
                or identity(result["experiment_id"]) != self.config.experiment_id):
            raise ContractError("Study context does not match configured experiment")
        identity(result["study_id"])
        normalized(result["study_name"], 120)
        result["compatibility"] = validate_compatibility(result["compatibility"])
        subjects = result["subjects"]
        if type(subjects) is not list or not 1 <= len(subjects) <= 100:
            raise ContractError("Invalid study subject roster")
        for subject in subjects:
            if (type(subject) is not dict or set(subject) != {"subject_id", "trial_count"}
                    or canonical_subject(subject["subject_id"]) != subject["subject_id"]
                    or type(subject["trial_count"]) is not int
                    or not 1 <= subject["trial_count"] <= 50_000):
                raise ContractError("Invalid study subject roster")
        return result

    def next_block(self, subject_alias):
        subject = canonical_subject(subject_alias)
        value = _json(self.config.server_origin,
                      "/api/runner-device/subjects/" + quote(subject_alias, safe="") + "/next", self._token)
        package = BlockPackage.from_dict(value)
        if package.experiment_id != self.config.experiment_id or package.subject_id != subject:
            raise ContractError("Block does not match configured experiment and requested subject")
        return package

    def claim_attempt(self, package, attempt_id):
        if package.experiment_id != self.config.experiment_id:
            raise ContractError("Attempt package belongs to another experiment")
        result = _json(self.config.server_origin,
                       "/api/runner-device/blocks/" + identity(package.package_id) + "/attempts",
                       self._token, canonical_bytes({"attempt_id": identity(attempt_id)}), expected_status=201)
        expected = {"attempt_id": attempt_id, "package_id": package.package_id,
                    "package_sha256": package.package_sha256, "experiment_id": package.experiment_id,
                    "subject_id": package.subject_id, "device_id": self.config.device_id}
        if type(result) is not dict or any(result.get(key) != value for key, value in expected.items()):
            raise ContractError("Attempt reservation identity mismatch")
        return result

    def attempt_status(self, attempt_id):
        result = _json(self.config.server_origin, "/api/runner-device/attempts/" + identity(attempt_id), self._token)
        if type(result) is not dict or result.get("attempt_id") != attempt_id:
            raise ContractError("Attempt status identity mismatch")
        return result

    def _attempt_post(self, attempt_id, suffix, body):
        encoded = canonical_bytes(body)
        if len(encoded) > 2 * 1024 * 1024:
            raise ContractError("Runner request exceeds server body limit")
        return _json(self.config.server_origin, "/api/runner-device/attempts/" + identity(attempt_id) + suffix,
                     self._token, encoded)

    def append_events(self, attempt_id, events):
        if type(events) is not list or not events or len(events) > 16:
            raise ContractError("Send one to sixteen journal events per batch")
        return self._attempt_post(attempt_id, "/events", {"events": events})

    def upload_chunk(self, attempt_id, artifact, chunk_index, content):
        if (type(content) is not bytes or len(content) > 1024 * 1024
                or type(chunk_index) is not int or chunk_index < 0):
            raise ContractError("Invalid bounded artifact chunk")
        body = {"path": artifact["path"], "chunk_index": chunk_index, "file_bytes": artifact["bytes"],
                "file_sha256": artifact["sha256"], "chunk_sha256": hashlib.sha256(content).hexdigest(),
                "data_base64": base64.b64encode(content).decode("ascii")}
        return self._attempt_post(attempt_id, "/artifacts/" + identity(artifact["artifact_id"]) + "/chunks", body)

    def finalize_attempt(self, attempt_id, manifest):
        return self._attempt_post(attempt_id, "/finalize", manifest)

    def iter_media(self, package, trial, *, offset=0):
        if (package.experiment_id != self.config.experiment_id or trial not in package.trials
                or type(offset) is not int or not 0 <= offset < trial.media_bytes):
            raise ContractError("Media request identity or offset is invalid")
        path = ("/api/runner-device/blocks/" + identity(package.package_id)
                + f"/trials/{trial.trial_index}/media")
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with _request(self.config.server_origin, path, token=self._token, headers=headers) as response:
            if offset and response.status == 200:
                raise RangeNotSupported("Range ignored; restart from byte zero")
            if response.status != (206 if offset else 200):
                raise ApiError("Unexpected media response status")
            expected = trial.media_bytes - offset
            if offset and response.headers.get("Content-Range") != (
                    f"bytes {offset}-{trial.media_bytes - 1}/{trial.media_bytes}"):
                raise ApiError("Invalid media Content-Range")
            length = response.headers.get("Content-Length")
            if length is not None and length != str(expected):
                raise ApiError("Invalid media Content-Length")
            received = 0
            while True:
                chunk = response.read(min(1024 * 1024, expected - received + 1))
                if not chunk:
                    break
                received += len(chunk)
                if received > expected:
                    raise ApiError("Media exceeds sealed byte length")
                yield chunk
            if received != expected:
                raise ApiError("Media shorter than sealed byte length")
