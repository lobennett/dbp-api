"""In-memory cookie sessions and bounded, non-redirecting stdlib HTTP."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import hashlib
from http.client import HTTPException, HTTPResponse
from http.cookiejar import CookieJar
import json
import math
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import (
    ExperimentSpec, JSONObject, JSONValue, Media, MediaType, MetricFilter,
    UnsupportedMediaError, Video, identifier, media_from_row,
)
from .workflow import Experiment, Assignments, Selection, subject_ids


class ApiError(RuntimeError):
    """Sanitized request failure; HTTP status is available without response text."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


def _origin(value: str) -> str:
    message = "Use an HTTPS origin (HTTP only for localhost), without credentials, path, query or fragment"
    if not isinstance(value, str) or any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(message) from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or "?" in value or "#" in value or "\\" in value
            or (port is not None and port == 0)
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError(message)
    return value.rstrip("/")


def _object(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


class Client:
    """Synchronous client. Sessions stay in memory; instances are not thread-safe."""

    def __init__(self, origin: str, *, timeout: float = 30,
                 max_json_bytes: int = 16 * 1024 * 1024,
                 max_media_bytes: int = 4 * 1024 * 1024 * 1024) -> None:
        self.origin = _origin(origin)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        for limit in (max_json_bytes, max_media_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("Response size limits must be positive integers")
        self.timeout = timeout
        self.max_json_bytes = max_json_bytes
        self.max_media_bytes = max_media_bytes
        self._cookies = CookieJar()
        self._csrf: str | None = None
        self._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self._cookies), _NoRedirect())

    def close(self) -> None:
        """Forget local credentials; use logout() to revoke the server session."""
        self._cookies.clear()
        self._csrf = None

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def _request(self, method: str, path: str, body: JSONObject | None = None,
                 *, media: bool = False) -> Iterator[HTTPResponse]:
        headers = {"Accept": "video/mp4" if media else "application/json", "Accept-Encoding": "identity"}
        if method != "GET" and path != "/api/auth/login":
            if self._csrf is None:
                raise ApiError("Sign in before making a modifying request")
            headers["X-CSRF-Token"] = self._csrf
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=encoded, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in (200, 201):
                    raise ApiError("Unexpected HTTP response", status=response.status)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ApiError("Encoded responses are not supported")
                yield response
        except HTTPError as error:
            status = error.code
            detail = None
            try:
                if status in (400, 409) and not path.startswith("/api/auth/"):
                    raw = error.read(4097)
                    if len(raw) <= 4096:
                        value = json.loads(raw)
                        if isinstance(value, dict) and isinstance(value.get("detail"), str):
                            printable = "".join(character if character.isprintable() else " "
                                                for character in value["detail"])
                            detail = " ".join(printable.split())[:512]
            except (ValueError, OSError, HTTPException):
                pass
            finally:
                error.close()
            if status == 401:
                self.close()
            message = f"Server returned HTTP {status}"
            raise ApiError(message + (f": {detail}" if detail else "; response body withheld"), status=status) from None
        except (OSError, URLError, HTTPException):
            raise ApiError("Request failed; connection details withheld") from None

    def _json(self, method: str, path: str, body: JSONObject | None = None) -> JSONObject:
        with self._request(method, path, body) as response:
            raw = response.read(self.max_json_bytes + 1)
        if len(raw) > self.max_json_bytes:
            raise ApiError("JSON exceeds configured size limit")
        try:
            value = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant,
                               parse_float=_finite_float)
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError("Invalid JSON object response") from None

    def _session(self, value: JSONObject) -> JSONObject:
        csrf = value.get("csrf_token")
        if not isinstance(csrf, str) or not re.fullmatch(r"[!-~]{1,1024}", csrf):
            self.close()
            raise ApiError("Session response has no valid CSRF token")
        self._csrf = csrf
        return value

    def login(self, username: str, password: str) -> JSONObject:
        self.close()
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise ValueError("username and password must be nonempty strings")
        try:
            return self._session(self._json("POST", "/api/auth/login", {"username": username, "password": password}))
        except BaseException:
            self.close()
            raise

    def session(self) -> JSONObject:
        return self._session(self._json("GET", "/api/auth/session"))

    def logout(self) -> JSONObject:
        try:
            return self._json("POST", "/api/auth/logout", {})
        finally:
            self.close()

    def metrics(self) -> JSONObject:
        return self._json("GET", "/api/v1/metrics")

    @staticmethod
    def _query(filters: Sequence[MetricFilter], content_query: str, corpus: str,
               mode: str, version: str | None, custom_metrics: Sequence[JSONObject],
               search_version: str | None, relevance_min: float | None,
               relevance_max: float | None, cpu_pool: str | None) -> JSONObject:
        body: JSONObject = {"filters": [item.to_dict() for item in filters], "content_query": content_query,
                            "corpus": corpus, "mode": mode, "custom_metrics": list(custom_metrics)}
        if version is not None:
            body["version"] = version
        for key, value in (("search_version", search_version), ("relevance_min", relevance_min),
                           ("relevance_max", relevance_max), ("cpu_pool", cpu_pool)):
            if value is not None:
                body[key] = value
        return body

    def query_media(self, *, filters: Sequence[MetricFilter] = (), content_query: str = "",
                    corpus: str = "both", mode: str = "keyword", version: str | None = None,
                    limit: int = 100, cursor: str | None = None,
                    custom_metrics: Sequence[JSONObject] = (), search_version: str | None = None,
                    relevance_min: float | None = None, relevance_max: float | None = None,
                    cpu_pool: str | None = None, experiment_id: str | None = None,
                    subject_id: str = "all", view_seed: str | None = None) -> JSONObject:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        body = self._query(filters, content_query, corpus, mode, version, custom_metrics,
                           search_version, relevance_min, relevance_max, cpu_pool)
        body["limit"] = limit
        if experiment_id is not None:
            if not re.fullmatch(r"all|subject-(?:00[1-9]|0[1-9][0-9]|100)", subject_id):
                raise ValueError("Choose all or a subject ID from the experiment roster")
            experiment = self.experiment(experiment_id)
            body["study"] = {"collection_id": experiment["collection_id"],
                             "study_id": experiment_id, "subject_id": subject_id}
            if version is None:
                body["version"] = experiment["version"]
            if not custom_metrics:
                recipe = experiment.get("recipe")
                if isinstance(recipe, dict):
                    body["custom_metrics"] = recipe.get("custom_metrics", [])
        elif subject_id != "all":
            raise ValueError("subject_id requires experiment_id")
        if view_seed is not None:
            body["view_seed"] = view_seed
        if cursor is not None:
            body["cursor"] = cursor
        return self._json("POST", "/api/v1/media/query", body)

    def preview_custom_metric(self, query: str, *, version: str, corpus: str = "both",
                              method: str = "keyword_bm25_v1") -> JSONObject:
        return self._json("POST", "/api/metrics/custom/preview",
                          {"query": query, "version": version, "corpus": corpus, "method": method})

    def create_experiment(self, *, name: str, seed: str,
                          filters: Sequence[MetricFilter] = (), content_query: str = "", corpus: str = "both",
                          media_type: MediaType | type[Media] = Video) -> Experiment:
        """Pin a selection recipe. assign() subsequently saves subject assignments."""
        if media_type not in ("video", Video):
            raise UnsupportedMediaError("Only videos are supported by this server; images are a future modality")
        ExperimentSpec(name, seed, 1, 1)
        result = self.query_media(filters=filters, content_query=content_query, corpus=corpus, limit=1)
        version, search_version = result.get("version"), result.get("search_version")
        if not isinstance(version, str) or not version or (search_version is not None and not isinstance(search_version, str)):
            raise ApiError("Query returned invalid dataset or search version")
        selection = Selection(tuple(filters), content_query, corpus, version, search_version)
        return Experiment(self, name, seed, selection)

    def assignments(self, experiment_id: str) -> Assignments:
        """Open existing published assignments without sampling or changing them."""
        detail = self.experiment(experiment_id)
        publication = detail.get("publication")
        if not isinstance(publication, dict) or "subjects" not in publication:
            raise ValueError("Publish this experiment before opening its assignments")
        return Assignments(self, experiment_id, subject_ids(publication))

    def _create_experiment(self, spec: ExperimentSpec, *, media_type: MediaType | type[Media] = "video",
                          filters: Sequence[MetricFilter] = (), content_query: str = "", corpus: str = "both",
                          mode: str = "keyword", version: str | None = None,
                          custom_metrics: Sequence[JSONObject] = (), search_version: str | None = None,
                          relevance_min: float | None = None, relevance_max: float | None = None,
                          cpu_pool: str | None = None) -> JSONObject:
        if media_type not in ("video", Video):
            raise UnsupportedMediaError("Only video experiments are supported; images and stimuli are future modalities")
        if not isinstance(version, str) or not version:
            raise ValueError("Creation requires version; pass query_media(...)[\"version\"]")
        body = self._query(filters, content_query, corpus, mode, version, custom_metrics,
                           search_version, relevance_min, relevance_max, cpu_pool)
        body.update(settings=spec.to_dict(), media_type="video")
        return self._json("POST", "/api/v1/experiments", body)

    def experiments(self, *, limit: int | None = None, offset: int | None = None) -> JSONObject:
        parameters = []
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("limit must be an integer from 1 to 100")
            parameters.append(f"limit={limit}")
        if offset is not None:
            if type(offset) is not int or not 0 <= offset <= 100000:
                raise ValueError("offset must be an integer from 0 to 100000")
            parameters.append(f"offset={offset}")
        suffix = "?" + "&".join(parameters) if parameters else ""
        return self._json("GET", "/api/v1/experiments" + suffix)

    def experiment(self, experiment_id: str) -> JSONObject:
        return self._json("GET", "/api/v1/experiments/" + identifier(experiment_id))

    def publish(self, experiment_id: str) -> JSONObject:
        return self._json("POST", "/api/v1/experiments/" + identifier(experiment_id) + "/publish", {})

    @staticmethod
    def _subject_path(experiment_id: str, subject_id: str) -> str:
        return "/api/v1/experiments/" + identifier(experiment_id) + "/subjects/" + identifier(subject_id)

    def subject_manifest(self, experiment_id: str, subject_id: str) -> JSONObject:
        manifest = self._json("GET", self._subject_path(experiment_id, subject_id) + "/manifest")
        _manifest_trials(manifest, experiment_id, subject_id)
        return manifest

    def _download(self, path: str, destination: Path) -> None:
        with self._request("GET", path, media=True) as response:
            if response.headers.get_content_type() != "video/mp4":
                raise ApiError("Trial media response must be video/mp4")
            length = response.headers.get("Content-Length")
            if length is None or not length.isdecimal() or int(length) > self.max_media_bytes:
                raise ApiError("Invalid or excessive media Content-Length")
            expected_hash = response.headers.get("X-Content-SHA256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise ApiError("Media response requires X-Content-SHA256")
            digest = hashlib.sha256()
            received = 0
            with destination.open("xb") as output:
                while chunk := response.read(min(1024 * 1024, self.max_media_bytes - received + 1)):
                    received += len(chunk)
                    if received > self.max_media_bytes:
                        raise ApiError("Media exceeds configured size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if not received or (length is not None and received != int(length)):
                raise ApiError("Incomplete media response")
            if digest.hexdigest() != expected_hash:
                raise ApiError("Media SHA-256 mismatch")

    def download_subject(self, experiment_id: str, subject_id: str, destination: str | Path) -> Path:
        """Download all trials into a NEW directory, removing it on any failure.

        manifest.json wraps the untouched server manifest and a trial-ID/path map.
        No server hash is recalculated or applied to the augmented local document.
        Missing parent directories are created; an existing destination is never replaced.
        """
        path = self._subject_path(experiment_id, subject_id)
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError("Download destination already exists")
        manifest = self.subject_manifest(experiment_id, subject_id)
        trial_ids = _manifest_trials(manifest, experiment_id, subject_id)
        target.mkdir(mode=0o700, parents=True)
        try:
            files: JSONObject = {}
            for index, trial_id in enumerate(trial_ids, 1):
                filename = f"{index:06d}-{trial_id}.mp4"
                self._download(path + "/trials/" + trial_id + "/media", target / filename)
                files[trial_id] = filename
            local = target / "manifest.json"
            with local.open("x", encoding="utf-8") as output:
                json.dump({"manifest": manifest, "files": files}, output, indent=2, allow_nan=False)
            return local
        except BaseException:
            shutil.rmtree(target)
            raise


def _manifest_trials(manifest: JSONObject, experiment_id: str, subject_id: str) -> list[str]:
    expected_hash = manifest.get("manifest_sha256")
    if (manifest.get("schema_version") != "dbp-experiment-v1"
            or manifest.get("experiment_id") != experiment_id or manifest.get("subject_id") != subject_id
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ApiError("Manifest schema, identity or hash is invalid")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != expected_hash:
        raise ApiError("Manifest SHA-256 mismatch")
    blocks = manifest.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ApiError("Manifest requires blocks")
    trial_ids: list[str] = []
    seen: set[str] = set()
    for index, block in enumerate(blocks, 1):
        if (not isinstance(block, dict) or type(block.get("block_index")) is not int
                or block["block_index"] != index):
            raise ApiError("Manifest block order or trials are invalid")
        trials = block.get("trials")
        if not isinstance(trials, list):
            raise ApiError("Manifest block trials must be a list")
        for trial in trials:
            if not isinstance(trial, dict) or "segment" not in trial or "media_id" not in trial:
                raise ApiError("Invalid manifest trial")
            trial_id = identifier(trial.get("trial_id"))
            media = media_from_row(trial)
            if trial_id in seen or trial.get("role") not in ("parent", "repeat", "foil"):
                raise ApiError("Duplicate trial ID or invalid trial role")
            if media.media_type != "video" or trial.get("media_type") != "video":
                raise UnsupportedMediaError("Only video trial downloads are supported")
            segment = trial["segment"]
            if trial["role"] == "foil" and segment is None:
                raise ApiError("Foils require a segment")
            if trial["role"] == "repeat" and segment is not None:
                raise ApiError("Repeat trials must have no segment")
            if segment is not None:
                if not isinstance(segment, dict) or set(segment) != {"start_seconds", "end_seconds"}:
                    raise ApiError("Invalid segment")
                start, end = segment["start_seconds"], segment["end_seconds"]
                if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                        or isinstance(start, bool) or isinstance(end, bool) or not 0 <= start < end):
                    raise ApiError("Invalid segment bounds")
            seen.add(trial_id)
            trial_ids.append(trial_id)
    if not trial_ids:
        raise ApiError("Manifest requires trials")
    return trial_ids
