"""Standalone strict mirror of the server's integration block wire contract."""

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


MAX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_BLOCK_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
IDENTITY = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CONDITIONS = {"parent": "new-integration-parent", "repeat": "old-integration-repeat",
              "foil": "new-integration-foil"}


class ContractError(ValueError):
    pass


def is_finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid canonical JSON") from None


def strict_json(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ContractError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ContractError("Non-finite JSON value")

    if len(raw) > MAX_JSON_BYTES:
        raise ContractError("JSON exceeds size limit")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid JSON response") from None


def seal_document(value, field):
    if type(value) is not dict or field in value:
        raise ContractError("Document already sealed or not an object")
    copied = strict_json(canonical_bytes(value))
    copied[field] = hashlib.sha256(canonical_bytes(value)).hexdigest()
    return copied


def verify_document(value, field):
    if type(value) is not dict:
        raise ContractError("Expected sealed object")
    copied = strict_json(canonical_bytes(value))
    digest = copied.pop(field, None)
    if (type(digest) is not str or not SHA256.fullmatch(digest)
            or hashlib.sha256(canonical_bytes(copied)).hexdigest() != digest):
        raise ContractError("Document digest mismatch")
    return copied


def identity(value):
    if type(value) is not str or not IDENTITY.fullmatch(value):
        raise ContractError("Invalid hexadecimal identity")
    return value


def normalized(value, limit=None):
    if (type(value) is not str or not value or not value.isprintable()
            or value != value.strip() or (limit is not None and len(value) > limit)):
        raise ContractError("Expected normalized printable string")
    return value


def canonical_subject(alias):
    if type(alias) is not str or not re.fullmatch(r"(?:s|subject-)(?:0[0-9]{2}|100)", alias):
        raise ContractError("Use s001..s100 or subject-001..subject-100")
    if alias.endswith("000"):
        raise ContractError("Subject zero is not assigned")
    return "subject-" + alias[-3:]


@dataclass(frozen=True)
class Trial:
    trial_index: int
    clip_id: str
    role: str
    condition: str
    media_path: str
    media_bytes: int
    media_sha256: str


@dataclass(frozen=True)
class BlockPackage:
    schema_version: str
    mode: str
    pgl_ready: bool
    package_id: str
    experiment_id: str
    subject_id: str
    block_id: str
    trials: tuple[Trial, ...]
    package_sha256: str

    @classmethod
    def from_dict(cls, value):
        document = verify_document(value, "package_sha256")
        if set(document) != set(cls.__dataclass_fields__) - {"package_sha256"}:
            raise ContractError("Invalid block field set")
        if document["mode"] != "integration_test" or document["pgl_ready"] is not False:
            raise ContractError("Only integration packages with pgl_ready false are accepted")
        if document["schema_version"] != "dbp-pgl-block-v1":
            raise ContractError("Unsupported block schema")
        identities = [identity(document[field]) for field in
                      ("package_id", "experiment_id", "block_id")]
        if len(set(identities)) != 3:
            raise ContractError("Package identities must be distinct")
        normalized(document["subject_id"])
        trials = document["trials"]
        if type(trials) is not list or not 1 <= len(trials) <= 50_000:
            raise ContractError("Invalid trial count")
        seen = set()
        paths, digests, clips = {}, {}, {}
        for index, trial in enumerate(trials):
            if type(trial) is not dict or set(trial) != set(Trial.__dataclass_fields__):
                raise ContractError("Invalid trial field set")
            if type(trial["trial_index"]) is not int or trial["trial_index"] != index:
                raise ContractError("Trials must be contiguous and ordered from zero")
            clip = normalized(trial["clip_id"], 256)
            role = trial["role"]
            if (type(role) is not str or role not in CONDITIONS
                    or trial["condition"] != CONDITIONS[role] or (role, clip) in seen):
                raise ContractError("Invalid or duplicate role/clip identity")
            seen.add((role, clip))
            path = normalized(trial["media_path"], 512)
            parsed = PurePosixPath(path)
            if (":" in path or "\\" in path or parsed.is_absolute()
                    or path != parsed.as_posix() or path == "." or ".." in parsed.parts):
                raise ContractError("Unsafe local-relative media path")
            size, digest = trial["media_bytes"], trial["media_sha256"]
            if type(size) is not int or not 1 <= size <= MAX_MEDIA_BYTES:
                raise ContractError("Media size exceeds 32 MiB or is invalid")
            if type(digest) is not str or not SHA256.fullmatch(digest):
                raise ContractError("Invalid media SHA-256")
            for mapping, key, bound in [(paths, path, (size, digest)),
                                         (digests, digest, size),
                                         (clips, (clip, "foil" if role == "foil" else "full"), (size, digest))]:
                if key in mapping and mapping[key] != bound:
                    raise ContractError("Conflicting media identity")
                mapping[key] = bound
        if sum(digests.values()) > MAX_BLOCK_MEDIA_BYTES:
            raise ContractError("Distinct block media exceed 2 GiB")
        document["trials"] = tuple(Trial(**trial) for trial in trials)
        return cls(**document, package_sha256=value["package_sha256"])

    def to_dict(self):
        result = asdict(self)
        result["trials"] = list(result["trials"])
        return result
