"""Bounded same-origin requests; redirects and implicit retries are forbidden."""

from contextlib import contextmanager
import http.client
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import validate_origin, validate_token
from .models import (BlockPackage, ContractError, MAX_JSON_BYTES, canonical_subject,
                     identity, is_finite_number, normalized, strict_json)


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

    def next_block(self, subject_alias):
        subject = canonical_subject(subject_alias)
        value = _json(self.config.server_origin,
                      "/api/runner-device/subjects/" + quote(subject_alias, safe="") + "/next", self._token)
        package = BlockPackage.from_dict(value)
        if package.experiment_id != self.config.experiment_id or package.subject_id != subject:
            raise ContractError("Block does not match configured experiment and requested subject")
        return package

    def iter_media(self, package, trial, *, offset=0):
        if (package.experiment_id != self.config.experiment_id or trial not in package.trials
                or type(offset) is not int or not 0 <= offset < trial.media_bytes):
            raise ContractError("Media request identity or offset is invalid")
        path = ("/api/runner-device/blocks/" + identity(package.package_id) + "/media/"
                + quote(trial.clip_id, safe=""))
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
