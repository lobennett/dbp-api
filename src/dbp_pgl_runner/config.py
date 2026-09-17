"""Private, explicit integration-harness credential storage."""

from dataclasses import asdict, dataclass
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from urllib.parse import urlsplit

from .models import ContractError, canonical_bytes, identity, strict_json


def validate_origin(value):
    try:
        if (type(value) is not str or not value.isascii() or not value.isprintable()
                or value != value.strip() or "\\" in value):
            raise ValueError
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (not hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or "?" in value or "#" in value or parsed.port == 0):
            raise ValueError
        if parsed.scheme == "http":
            if hostname != "localhost" and not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError
        elif parsed.scheme != "https":
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", hostname):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise ContractError("Use an HTTPS origin, or HTTP on literal loopback/localhost only") from None


def private_directory(path, *, create=True):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ContractError("Private directory cannot be a symlink")
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise ContractError("Private directory missing or inaccessible") from None
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()):
        raise ContractError("Private directory must be owned by this user with mode 0700")
    return path.resolve()


def preflight_config_directory(path):
    root = private_directory(path)
    if root.stat().st_mode & 0o700 != 0o700 or not os.access(root, os.W_OK | os.X_OK):
        raise ContractError("Configuration directory must be writable with mode 0700")
    try:
        with tempfile.TemporaryFile(dir=root) as probe:
            probe.write(b"storage preflight")
            probe.flush()
            os.fsync(probe.fileno())
        fsync_directory(root)
    except OSError:
        raise ContractError("Configuration directory is not writable") from None
    return root


def read_private(path, limit):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                    or metadata.st_uid != os.getuid() or metadata.st_size > limit):
                raise ContractError("Private file has unsafe permissions, type, or size")
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ContractError("Private file exceeds size limit")
        return content
    except OSError:
        raise ContractError("Private file missing or unsafe") from None


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fchmod(target.fileno(), mode)
            os.fsync(target.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_token(token):
    if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        raise ContractError("Invalid device credential")
    return token


@dataclass(frozen=True)
class RunnerConfig:
    server_origin: str
    device_id: str
    experiment_id: str
    token_ref: str

    def __post_init__(self):
        if self.server_origin != validate_origin(self.server_origin):
            raise ContractError("Server origin must be normalized without trailing slash")
        identity(self.device_id)
        identity(self.experiment_id)
        if type(self.token_ref) is not str or not re.fullmatch(r"token-[0-9a-f]{32}", self.token_ref):
            raise ContractError("Invalid token reference")

    @classmethod
    def load(cls, directory):
        root = private_directory(directory, create=False)
        value = strict_json(read_private(root / "config.json", 4096))
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("Invalid configuration fields")
        return cls(**value)

    def read_token(self, directory):
        root = private_directory(directory, create=False)
        try:
            return validate_token(read_private(root / self.token_ref, 128).decode("ascii"))
        except UnicodeError:
            raise ContractError("Invalid credential encoding") from None


def save_pairing(directory, origin, response, *, allow_file_token=False):
    if not allow_file_token:
        raise ContractError("Keychain backend pending; integration requires explicit --allow-file-token")
    token = validate_token(response.get("token"))
    config = RunnerConfig(validate_origin(origin), response["device_id"],
                          response["experiment_id"], "token-" + secrets.token_hex(16))
    root = private_directory(directory)
    token_path = root / config.token_ref
    atomic_write(token_path, token.encode("ascii"))
    atomic_write(root / "config.json", canonical_bytes(asdict(config)))
    return config
