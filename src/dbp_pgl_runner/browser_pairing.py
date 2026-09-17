"""Browser approval with memory-only proof, bounded polling and one exchange."""

from dataclasses import dataclass
import hashlib
import secrets
import threading
import time
import webbrowser
from weakref import WeakKeyDictionary

from .api import ApiError, RunnerApi
from .config import validate_origin
from .models import ContractError


@dataclass(frozen=True, eq=False)
class PendingAuthorization:
    origin: str
    request_id: str
    user_code: str
    verification_path: str
    expires_at: float
    interval: int


class BrowserPairing:
    def __init__(self):
        self._requests = WeakKeyDictionary()
        self._lock = threading.Lock()

    def start(self, origin, device_name):
        origin = validate_origin(origin)
        verifier = secrets.token_urlsafe(32)
        challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
        value = RunnerApi.start_authorization(origin, device_name, challenge)
        remaining = value["expires_at"] - time.time()
        if not 0 < remaining <= 900:
            raise ApiError("Authorization expired or exceeds its lifetime")
        pending = PendingAuthorization(origin=origin, **value)
        deadline = time.monotonic() + remaining
        try:
            opened = webbrowser.open(origin + pending.verification_path)
        except Exception:
            raise ApiError("Could not open authorization in the browser") from None
        if not opened:
            raise ApiError("Could not open authorization in the browser")
        with self._lock:
            self._requests[pending] = (verifier, deadline)
        return pending

    def wait(self, pending, cancel_event):
        """Return device_response and study_context only after live verification.

        A pending object belongs to this instance and can be waited on once.
        Cancellation and all failures consume the local proof, not a retry slot.
        """
        with self._lock:
            state = self._requests.pop(pending, None)
        if state is None:
            raise ApiError("Authorization is unavailable or already being handled")
        verifier, deadline = state

        def remaining():
            if cancel_event.is_set():
                raise ApiError("Authorization cancelled")
            seconds = min(pending.expires_at - time.time(), deadline - time.monotonic())
            if seconds <= 0:
                raise ApiError("Authorization expired")
            return seconds

        while True:
            cancel_event.wait(min(pending.interval, remaining()))
            status = RunnerApi.poll_authorization(pending.origin, pending.request_id, verifier,
                                                  timeout=min(30, remaining()))
            remaining()
            if status["expires_at"] != pending.expires_at:
                raise ContractError("Authorization expiry changed")
            if status["status"] == "denied":
                raise ApiError("Authorization denied")
            if status["status"] == "approved":
                break
        response = RunnerApi.exchange_authorization(pending.origin, pending.request_id, verifier,
                                                    timeout=min(30, remaining()))
        remaining()
        if response["experiment_id"] != status["experiment_id"]:
            raise ContractError("Issued device does not match approved experiment")
        api = RunnerApi.from_device(pending.origin, response)
        api.verify_device(response, timeout=min(30, remaining()))
        context = api.study(timeout=min(30, remaining()))
        remaining()
        return {"device_response": response, "study_context": context}
