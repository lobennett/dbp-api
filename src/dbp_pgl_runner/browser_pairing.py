"""Browser approval with memory-only proof, bounded polling and one exchange."""

from dataclasses import dataclass, field
import hashlib
import secrets
import threading
import time
import webbrowser
from weakref import WeakKeyDictionary

from .api import ApiError, RequestBudget, RequestStopped, RunnerApi
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


@dataclass
class _AuthorizationState:
    verifier: str = field(repr=False)
    deadline: float
    next_poll_at: float
    active: bool = False


class BrowserPairing:
    def __init__(self):
        self._requests = WeakKeyDictionary()
        self._lock = threading.Lock()

    def start(self, origin, device_name, *, cancel_event=None):
        origin = validate_origin(origin)
        verifier = secrets.token_urlsafe(32)
        challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
        budget = RequestBudget(time.monotonic() + 30, cancel_event if cancel_event is not None else threading.Event())
        value = RunnerApi.start_authorization(origin, device_name, challenge, budget=budget)
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
            self._requests[pending] = _AuthorizationState(verifier, deadline,
                                                           time.monotonic() + pending.interval)
        return pending

    def wait(self, pending, cancel_event, *, reuse):
        """Resume polling, reuse a saved assignment, or exchange exactly once.

        The required reuse(origin, experiment_id) callback checks saved
        assignments and returns a profile name or None before any exchange.
        Reuse returns only profile_name; new pairing returns device_response and
        study_context after verification. Transport failures before exchange
        release waiter ownership without losing the proof or polling schedule.
        """
        if not callable(reuse):
            raise TypeError("Assignment reuse lookup must be callable")
        with self._lock:
            state = self._requests.get(pending)
            if state is None or state.active:
                raise ApiError("Authorization is unavailable or already being handled")
            state.active = True
        budget = RequestBudget(state.deadline, cancel_event, pending.expires_at)

        def consume():
            with self._lock:
                self._requests.pop(pending, None)

        def remaining():
            return budget.remaining()

        try:
            while True:
                delay = max(0, state.next_poll_at - time.monotonic())
                cancel_event.wait(min(delay, remaining()))
                remaining()
                try:
                    status = RunnerApi.poll_authorization(pending.origin, pending.request_id, state.verifier,
                                                          timeout=min(30, remaining()), budget=budget)
                finally:
                    state.next_poll_at = time.monotonic() + pending.interval
                remaining()
                if status["expires_at"] != pending.expires_at:
                    raise ContractError("Authorization expiry changed")
                if status["status"] == "denied":
                    consume()
                    raise ApiError("Authorization denied")
                if status["status"] == "approved":
                    break
            name = reuse(pending.origin, status["experiment_id"])
            remaining()
            if name is not None:
                consume()
                return {"profile_name": name}
            consume()
            response = RunnerApi.exchange_authorization(pending.origin, pending.request_id, state.verifier,
                                                        timeout=min(30, remaining()), budget=budget)
            remaining()
            if response["experiment_id"] != status["experiment_id"]:
                raise ContractError("Issued device does not match approved experiment")
            api = RunnerApi.from_device(pending.origin, response)
            api.verify_device(response, timeout=min(30, remaining()), budget=budget)
            context = api.study(timeout=min(30, remaining()), budget=budget)
            remaining()
            return {"device_response": response, "study_context": context}
        except (ContractError, RequestStopped):
            consume()
            raise
        finally:
            with self._lock:
                state.active = False
