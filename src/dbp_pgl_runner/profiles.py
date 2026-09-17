"""Private named connections without migrating or overwriting legacy credentials."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

from .api import RunnerApi
from .config import (RunnerConfig, fsync_directory, preflight_config_directory,
                     private_directory, save_pairing, validate_origin, validate_token)
from .models import ContractError, canonical_bytes, normalized, strict_json


_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")


def _name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ContractError("Profile names require 1–48 lowercase letters, digits or hyphens, starting with a letter")
    return value


@contextmanager
def _pairing_lock(root):
    try:
        descriptor = os.open(root / ".pairing.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise ContractError("Profile pairing lock is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            raise ContractError("Profile pairing lock must be a private regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("Pairing is already in progress for this profile") from None
        yield
    finally:
        os.close(descriptor)


def _publish_pairing(root, origin, response):
    with tempfile.TemporaryDirectory(prefix=".pairing-", dir=root) as temporary:
        staging = Path(temporary)
        config = save_pairing(staging, origin, response, allow_file_token=True)
        token_path = root / config.token_ref
        token_published = config_published = False
        try:
            os.link(staging / config.token_ref, token_path, follow_symlinks=False)
            token_published = True
            fsync_directory(root)
            os.link(staging / "config.json", root / "config.json", follow_symlinks=False)
            config_published = True
            fsync_directory(root)
        except FileExistsError:
            raise ContractError("Profile configuration appeared during pairing; it was not overwritten") from None
        finally:
            if token_published and not config_published:
                token_path.unlink()
                fsync_directory(root)
    fsync_directory(root)
    return config


class ConnectionProfiles:
    """Resolve profile directories and pair new connections without replacement.

    Lookup and listing never create directories. The reserved default profile is
    the original base directory; other profiles live below base/profiles/name.
    Listing checks config structure and permissions without reading credentials.
    """

    def __init__(self, base):
        self.base = Path(base).expanduser().absolute()

    def directory(self, name="default"):
        name = _name(name)
        paths = [self.base]
        if name != "default":
            paths.extend((self.base / "profiles", self.base / "profiles" / name))
        for path in paths:
            if os.path.lexists(path):
                private_directory(path, create=False)
        return paths[-1]

    def names(self):
        root = self.directory()
        if not root.exists():
            return []
        result = []
        try:
            RunnerConfig.load(root)
        except ContractError:
            pass
        else:
            result.append("default")
        profiles = root / "profiles"
        if not os.path.lexists(profiles):
            return result
        private_directory(profiles, create=False)
        names = []
        with os.scandir(profiles) as entries:
            for entry in entries:
                if entry.name == "default":
                    continue
                try:
                    RunnerConfig.load(self.directory(entry.name))
                except ContractError:
                    continue
                names.append(entry.name)
        return result + sorted(names)

    def publish(self, origin, device_response, study_context):
        """Verify live authority in memory, then publish or reuse without replacement.

        Calling this method opts into the same private-file credential storage
        as manual pairing. No credential or directory is written before identity
        and authoritative study verification have both succeeded.
        """
        origin = validate_origin(origin)
        device_response = strict_json(canonical_bytes(device_response))
        study_context = strict_json(canonical_bytes(study_context))
        api = RunnerApi.from_device(origin, device_response)
        api.verify_device(device_response)
        context = api.study()
        if canonical_bytes(context) != canonical_bytes(study_context):
            raise ContractError("Study context does not match the verified study")
        title = unicodedata.normalize("NFKD", context["study_name"]).encode("ascii", "ignore").decode().lower()
        stem = re.sub(r"[^a-z0-9]+", "-", title).strip("-") or "study"
        if not stem[0].isalpha():
            stem = "study-" + stem
        name = stem[:39].rstrip("-") + "-" + context["experiment_id"][:8]
        root = self.directory(name)
        private_directory(self.base)
        private_directory(self.base / "profiles")
        root = preflight_config_directory(root)
        fsync_directory(root.parent)
        fsync_directory(self.base)
        fsync_directory(self.base.parent)
        with _pairing_lock(root):
            if os.path.lexists(root / "config.json"):
                existing = RunnerConfig.load(root)
                if (existing.server_origin != origin or existing.device_id != device_response["device_id"]
                        or existing.experiment_id != device_response["experiment_id"]):
                    raise ContractError("Profile conflicts with an existing connection; it was not overwritten")
                existing.read_token(root)
            else:
                _publish_pairing(root, origin, device_response)
        return name

    def connect(self, name, origin, pairing_code, device_name, *, allow_file_token=False):
        """Pair once into an unused profile; return device and experiment IDs only.

        Config publication never replaces a file, even if a non-cooperating writer
        creates one during the exchange. Private staging uses hard links, not
        credential copies, and is removed before returning. A failed exchange is
        never retried automatically because the remote code may have been used.
        """
        if allow_file_token is not True:
            raise ContractError("Explicit private-file credential storage consent is required")
        root = self.directory(name)
        origin = validate_origin(origin)
        validate_token(pairing_code)
        normalized(device_name, 160)
        private_directory(self.base)
        if name != "default":
            private_directory(self.base / "profiles")
        root = preflight_config_directory(root)
        fsync_directory(root.parent)
        fsync_directory(self.base)
        fsync_directory(self.base.parent)
        with _pairing_lock(root):
            if os.path.lexists(root / "config.json"):
                raise ContractError("Profile already contains a configuration; choose a new name")
            before = root.stat(follow_symlinks=False)
            response = RunnerApi.pair(origin, pairing_code, device_name)
            current = self.directory(name).stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ContractError("Profile directory changed during pairing")
            config = _publish_pairing(root, origin, response)
            return {"device_id": config.device_id, "experiment_id": config.experiment_id}
