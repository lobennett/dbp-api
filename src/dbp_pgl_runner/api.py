"""Bounded same-origin requests; redirects and implicit retries are forbidden."""

from contextlib import contextmanager
import base64
import hashlib
import http.client
import json
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (HTTPHandler, HTTPSHandler, HTTPRedirectHandler,
                            ProxyHandler, Request, build_opener)

from .config import RunnerConfig, validate_origin, validate_token
from .compatibility import validate_compatibility
from .models import (BlockPackage, ContractError, MAX_JSON_BYTES, canonical_subject,
                     canonical_bytes, identity, is_finite_number, normalized, strict_json)


class ApiError(RuntimeError):
    pass


class RangeNotSupported(ApiError):
    pass


class RequestStopped(ApiError):
    pass


class RequestBudget:
    """Bound caller latency and shut down active sockets on cancellation/expiry."""

    def __init__(self, deadline, cancel_event, expires_at=None):
        self.deadline = deadline
        self.cancel_event = cancel_event
        self.expires_at = expires_at

    def remaining(self):
        if self.cancel_event.is_set():
            raise RequestStopped("Authorization cancelled")
        seconds = self.deadline - time.monotonic()
        if self.expires_at is not None:
            seconds = min(seconds, self.expires_at - time.time())
        if seconds <= 0:
            raise RequestStopped("Authorization expired")
        return seconds

    def run(self, operation):
        self.remaining()
        completed, stopped = threading.Event(), threading.Event()
        lock = threading.Lock()
        sockets, result, errors = set(), [], []

        def register(connection):
            try:
                with lock:
                    self.remaining()
                    if stopped.is_set():
                        raise RequestStopped("Authorization request stopped")
                    sockets.add(connection)
            except BaseException:
                connection.close()
                raise

        def worker():
            try:
                self.remaining()
                result.append(operation(register))
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        thread = threading.Thread(target=worker, name="runner-authorization-http", daemon=True)
        thread.start()
        try:
            while not completed.wait(min(0.02, self.remaining())):
                pass
            self.remaining()
            if errors:
                raise errors[0]
            return result[0]
        finally:
            with lock:
                stopped.set()
                connections = tuple(sockets)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _InterruptibleHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host,
                                              do_handshake_on_connect=False)
        self.register(self.sock)
        self.sock.do_handshake()


def _bounded_handlers(register):
    def connection(connection_type, host, **kwargs):
        instance = connection_type(host, **kwargs)
        create = instance._create_connection

        def create_registered(*args, **options):
            connected = create(*args, **options)
            register(connected)
            return connected

        instance._create_connection = create_registered
        instance.register = register
        return instance

    class BoundedHTTPHandler(HTTPHandler):
        def http_open(self, request):
            return self.do_open(lambda host, **kwargs: connection(http.client.HTTPConnection, host, **kwargs),
                                request)

    class BoundedHTTPSHandler(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(lambda host, **kwargs: connection(_InterruptibleHTTPSConnection, host, **kwargs),
                                request)

    return BoundedHTTPHandler(), BoundedHTTPSHandler()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


@contextmanager
def _request(origin, path, *, token=None, body=None, headers=None, timeout=30, register=None):
    origin = validate_origin(origin)
    request_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    if token is not None:
        request_headers["Authorization"] = "Bearer " + validate_token(token)
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(origin + path, data=body, headers=request_headers)
    handlers = _bounded_handlers(register) if register is not None else ()
    opener = build_opener(ProxyHandler({}), _NoRedirect(), *handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ApiError("Encoded response refused")
            yield response
    except HTTPError as error:
        status = error.code
        error.close()
        raise ApiError(f"Runner server returned HTTP {status}; response body withheld") from None
    except (OSError, URLError, http.client.HTTPException, ValueError):
        raise ApiError("Runner request failed; details withheld to protect credentials") from None


def _json(origin, path, token=None, body=None, expected_status=200, *, timeout=30,
          budget=None, _register=None):
    if budget is not None:
        return budget.run(lambda register: _json(origin, path, token, body, expected_status,
                                                 timeout=min(timeout, budget.remaining()), _register=register))
    with _request(origin, path, token=token, body=body, timeout=timeout, register=_register) as response:
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


def _authorization_expiry(value):
    if not is_finite_number(value) or value <= 0:
        raise ContractError("Invalid authorization expiry")


def _authorization_body(request_id, verifier):
    identity(request_id)
    if type(verifier) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier):
        raise ContractError("Invalid authorization verifier")
    return canonical_bytes({"verifier": verifier})


class RunnerApi:
    def __init__(self, config, token):
        self.config = config
        self._token = validate_token(token)

    @staticmethod
    def start_authorization(origin, device_name, challenge, *, budget=None):
        normalized(device_name, 160)
        if type(challenge) is not str or not re.fullmatch(r"[0-9a-f]{64}", challenge):
            raise ContractError("Invalid authorization challenge")
        value = _json(origin, "/api/runner-device/authorizations",
                      body=canonical_bytes({"device_name": device_name, "challenge": challenge}),
                      expected_status=201, budget=budget)
        if type(value) is not dict or set(value) != {
                "request_id", "user_code", "verification_path", "expires_at", "interval"}:
            raise ContractError("Invalid authorization response fields")
        request_id = identity(value["request_id"])
        code = request_id[:4].upper() + "-" + request_id[4:8].upper()
        if (value["user_code"] != code
                or value["verification_path"] != "/runner/authorize?request=" + request_id):
            raise ContractError("Invalid authorization verification path or code")
        _authorization_expiry(value["expires_at"])
        if type(value["interval"]) is not int or not 1 <= value["interval"] <= 900:
            raise ContractError("Invalid authorization polling interval")
        return value

    @staticmethod
    def poll_authorization(origin, request_id, verifier, *, timeout=30, budget=None):
        body = _authorization_body(request_id, verifier)
        value = _json(origin, "/api/runner-device/authorizations/" + request_id + "/poll",
                      body=body, timeout=timeout, budget=budget)
        if type(value) is not dict or value.get("status") not in ("pending", "approved", "denied"):
            raise ContractError("Invalid authorization status")
        fields = {"status", "expires_at"}
        if value["status"] == "approved":
            fields.add("experiment_id")
        if set(value) != fields:
            raise ContractError("Invalid authorization poll fields")
        _authorization_expiry(value["expires_at"])
        if value["status"] == "approved":
            identity(value["experiment_id"])
        return value

    @staticmethod
    def exchange_authorization(origin, request_id, verifier, *, timeout=30, budget=None):
        body = _authorization_body(request_id, verifier)
        return _device(_json(origin, "/api/runner-device/authorizations/" + request_id + "/exchange",
                             body=body, expected_status=201, timeout=timeout, budget=budget), pairing=True)

    @classmethod
    def from_device(cls, origin, response):
        """Create a client from an in-memory credential without writing any files."""
        _device(response, pairing=True)
        config = RunnerConfig(validate_origin(origin), response["device_id"],
                              response["experiment_id"], "token-" + "0" * 32)
        return cls(config, response["token"])

    def verify_device(self, response, *, timeout=30, budget=None):
        _device(response, pairing=True)
        current = self.identity(timeout=timeout, budget=budget)
        if any(current[field] != response[field] for field in response if field != "token"):
            raise ContractError("Issued credential does not match verified device")
        return current

    @staticmethod
    def pair(origin, code, device_name):
        validate_token(code)
        normalized(device_name, 160)
        body = json.dumps({"code": code, "device_name": device_name}).encode()
        return _device(_json(origin, "/api/runner-device/pairings/exchange", body=body,
                             expected_status=201), pairing=True)

    def identity(self, *, timeout=30, budget=None):
        result = _device(_json(self.config.server_origin, "/api/runner-device/identity", self._token,
                               timeout=timeout, budget=budget))
        if (result["device_id"] != self.config.device_id
                or result["experiment_id"] != self.config.experiment_id):
            raise ContractError("Device response does not match configured identity")
        return result

    def next_block(self, subject_alias):
        subject = canonical_subject(subject_alias)
        value = _json(self.config.server_origin,
                      "/api/runner-device/subjects/" + quote(subject_alias, safe="") + "/next", self._token)
        package = BlockPackage.from_dict(value)
        if package.experiment_id != self.config.experiment_id or package.subject_id != subject:
            raise ContractError("Block does not match configured experiment and requested subject")
        return package

    def study(self, *, timeout=30, budget=None):
        value = _json(self.config.server_origin, "/api/runner-device/study", self._token,
                      timeout=timeout, budget=budget)
        if type(value) is not dict or set(value) != {
                "schema_version", "mode", "pgl_ready", "experiment_id", "study_id", "study_name",
                "compatibility", "subjects"}:
            raise ContractError("Invalid study response fields")
        if (value["schema_version"] != "dbp-pgl-study-v1"
                or value["mode"] != "integration_test" or value["pgl_ready"] is not False):
            raise ContractError("Study context must use the integration-only study schema")
        if identity(value["experiment_id"]) != self.config.experiment_id:
            raise ContractError("Study does not match configured experiment")
        identity(value["study_id"])
        normalized(value["study_name"], 120)
        value["compatibility"] = validate_compatibility(value["compatibility"])
        subjects = value["subjects"]
        if type(subjects) is not list or not 1 <= len(subjects) <= 100:
            raise ContractError("Study requires 1 to 100 subjects")
        seen = set()
        for subject in subjects:
            if type(subject) is not dict or set(subject) != {"subject_id", "trial_count"}:
                raise ContractError("Invalid study subject fields")
            subject_id = canonical_subject(subject["subject_id"])
            if subject_id != subject["subject_id"] or subject_id in seen:
                raise ContractError("Study subjects must be unique canonical identities")
            count = subject["trial_count"]
            if type(count) is not int or not 1 <= count <= 50_000:
                raise ContractError("Invalid study trial count")
            seen.add(subject_id)
        return value

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
