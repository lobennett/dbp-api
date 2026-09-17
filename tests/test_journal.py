import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dbp_pgl_runner.journal as journal_module
from dbp_pgl_runner.journal import Journal
from dbp_pgl_runner.models import ContractError
from tests.fixtures import seal


ATTEMPT = "a" * 32
MILESTONES = ("trial_loaded", "stimulus_started", "stimulus_finished",
              "response_started", "response_saved", "trial_completed")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "attempts"
        self.journal = Journal(self.root, ATTEMPT, 2)

    def complete_trial(self, index):
        for kind in MILESTONES:
            self.journal.append(kind, index)

    def test_canonical_chain_order_and_private_storage(self):
        self.journal.append("run_started", payload={"wall_time": 1, "label": "é"})
        self.complete_trial(0)
        self.complete_trial(1)
        self.journal.append("run_completed")
        previous = "0" * 64
        events = self.journal.read_events()
        self.assertEqual(len(events), 14)
        for sequence, (event, line) in enumerate(zip(events, self.journal.path.read_bytes().splitlines()), 1):
            self.assertEqual(set(event), {"schema_version", "attempt_id", "sequence",
                                          "previous_sha256", "kind", "trial_index",
                                          "payload", "event_sha256"})
            self.assertEqual(event["schema_version"], "dbp-pgl-event-v1")
            self.assertEqual(event["sequence"], sequence)
            self.assertEqual(event["previous_sha256"], previous)
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            self.assertEqual(line, encoded)
            body = {key: value for key, value in event.items() if key != "event_sha256"}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(event["event_sha256"], digest)
            previous = digest
        self.assertEqual(self.journal.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        status = Journal(self.root, ATTEMPT, 2).status()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["completed_trials"], 2)
        self.assertIsNone(status["next_trial_index"])
        self.assertFalse(status["needs_review"])

    def test_fsync_before_success_and_exact_tail_retry_is_idempotent(self):
        original_fsync = os.fsync
        synced = []

        def record_sync(descriptor):
            original_fsync(descriptor)
            synced.append(os.fstat(descriptor).st_size)

        with patch("dbp_pgl_runner.journal.os.fsync", side_effect=record_sync):
            event = self.journal.append("run_started", payload={"label": "start"})
        self.assertIn(self.journal.path.stat().st_size, synced)
        replay = Journal(self.root, ATTEMPT, 2).append("run_started", payload={"label": "start"})
        self.assertEqual(replay, event)
        self.assertEqual(len(self.journal.read_events()), 1)
        with self.assertRaises(ContractError):
            self.journal.append("run_started", payload={"label": "changed"})

    def test_reopen_after_each_milestone_does_not_replay_exposure(self):
        self.journal.append("run_started")
        for index, kind in enumerate(MILESTONES):
            self.journal.append(kind, 0)
            reopened = Journal(self.root, ATTEMPT, 2)
            status = reopened.status()
            self.assertEqual(status["needs_review"], 1 <= index <= 4)
            self.assertEqual(status["can_resume"], index in (0, 5))
            self.assertEqual(status["next_trial_index"], 1 if index == 5 else 0)
            self.assertEqual(status["last_completed_trial"], 0 if index == 5 else None)
            if index >= 1:
                with self.assertRaises(ContractError):
                    reopened.append("trial_loaded", 0)
            self.assertEqual(len(reopened.read_events()), index + 2)

    def test_rejects_skipped_reordered_out_of_range_and_noninteger_trials(self):
        with self.assertRaises(ContractError):
            self.journal.append("trial_loaded", 0)
        self.journal.append("run_started")
        for kind, index in [("run_completed", None), ("trial_completed", 0),
                            ("trial_loaded", 1), ("trial_loaded", -1),
                            ("trial_loaded", 2), ("trial_loaded", True),
                            ("trial_loaded", 0.0), ("trial_loaded", None),
                            ("run_terminated", 0), ("stop_requested", None)]:
            with self.subTest(kind=kind, index=index), self.assertRaises(ContractError):
                self.journal.append(kind, index)
        self.complete_trial(0)
        with self.assertRaises(ContractError):
            self.journal.append("run_completed")
        with self.assertRaises(ContractError):
            self.journal.append("trial_loaded", 0)

    def test_termination_is_terminal_and_preserves_review_state(self):
        self.journal.append("run_started")
        self.journal.append("trial_loaded", 0)
        self.journal.append("stimulus_started", 0)
        terminal = self.journal.append("run_terminated", payload={"reason": "power loss"})
        self.assertEqual(self.journal.append("run_terminated", payload={"reason": "power loss"}), terminal)
        self.assertEqual(self.journal.status()["state"], "terminated")
        self.assertTrue(self.journal.status()["needs_review"])
        self.assertFalse(self.journal.status()["can_resume"])
        for kind in ("stimulus_finished", "trial_loaded", "run_completed"):
            with self.assertRaises(ContractError):
                self.journal.append(kind, 0 if kind != "run_completed" else None)

    def test_explicit_partial_tail_recovery_preserves_valid_prefix(self):
        self.journal.append("run_started")
        self.journal.append("trial_loaded", 0)
        self.journal.append("stimulus_started", 0)
        prefix = self.journal.path.read_bytes()
        with self.journal.path.open("ab") as target:
            target.write(b'{"schema_version":')
        for operation in (self.journal.read_events, self.journal.status,
                          lambda: self.journal.append("stimulus_finished", 0)):
            with self.assertRaises(ContractError):
                operation()
        self.assertEqual(self.journal.recover_incomplete_tail(), len(b'{"schema_version":'))
        self.assertEqual(self.journal.path.read_bytes(), prefix)
        self.assertEqual(self.journal.recover_incomplete_tail(), 0)
        self.assertTrue(self.journal.status()["needs_review"])

    def test_recovery_never_discards_complete_or_earlier_corruption(self):
        self.journal.append("run_started")
        valid = self.journal.path.read_bytes()
        for damaged in (valid.replace(b"run_started", b"run_tampered") + b'{',
                        valid + b'{}\n', valid.rstrip(b"\n"), valid + b'{}'):
            self.journal.path.write_bytes(damaged)
            with self.subTest(damaged=damaged), self.assertRaises(ContractError):
                self.journal.recover_incomplete_tail()
            self.assertEqual(self.journal.path.read_bytes(), damaged)

    def test_failed_fsync_never_reports_success_and_retry_does_not_duplicate(self):
        with patch("dbp_pgl_runner.journal.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises((OSError, ContractError)):
                self.journal.append("run_started")
        self.journal.append("run_started")
        self.assertEqual(len(self.journal.read_events()), 1)

    def test_payload_is_independent_bounded_strict_json_object(self):
        payload = {"nested": {"value": 1}}
        event = self.journal.append("run_started", payload=payload)
        payload["nested"]["value"] = 2
        event["payload"]["nested"]["value"] = 3
        self.assertEqual(self.journal.read_events()[0]["payload"], {"nested": {"value": 1}})
        for payload in ([], "bad", {"time": float("nan")}, {"large": "x" * 65536}, {1: "key"}):
            with self.subTest(payload=type(payload)), self.assertRaises(ContractError):
                self.journal.append("trial_loaded", 0, payload)

    def test_attempt_identity_count_binding_and_missing_journal_fail_closed(self):
        for attempt, count in [("../outside", 2), (ATTEMPT, 0), (ATTEMPT, True),
                               (ATTEMPT, 50001), (ATTEMPT, 1)]:
            with self.subTest(attempt=attempt, count=count), self.assertRaises(ContractError):
                Journal(self.root, attempt, count)
        self.journal.append("run_started")
        self.journal.path.unlink()
        with self.assertRaises(ContractError):
            Journal(self.root, ATTEMPT, 2)

    def test_symlink_and_unsafe_permissions_are_rejected(self):
        self.journal.path.chmod(0o644)
        with self.assertRaises(ContractError):
            self.journal.read_events()
        self.journal.path.unlink()
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"")
        self.journal.path.symlink_to(outside)
        with self.assertRaises(ContractError):
            self.journal.append("run_started")
        self.assertEqual(outside.read_bytes(), b"")

    def test_two_handles_serialize_and_use_current_disk_state(self):
        second = Journal(self.root, ATTEMPT, 2)
        self.journal.append("run_started")
        second.append("trial_loaded", 0)
        self.journal.append("stimulus_started", 0)
        self.assertEqual([event["sequence"] for event in second.read_events()], [1, 2, 3])

    def test_append_uses_validated_cache_but_detects_external_corruption(self):
        self.journal.append("run_started")
        self.journal.append("trial_loaded", 0)
        verifier = journal_module.verify_document
        verified_fields = []

        def verify(value, field):
            verified_fields.append(field)
            return verifier(value, field)

        with patch("dbp_pgl_runner.journal.verify_document", side_effect=verify):
            self.journal.append("stimulus_started", 0)
        self.assertNotIn("event_sha256", verified_fields)
        events = self.journal.read_events()
        events[0]["payload"]["mutated"] = True
        self.assertEqual(self.journal.read_events()[0]["payload"], {})
        raw = self.journal.path.read_bytes()
        self.journal.path.write_bytes(raw.replace(b"run_started", b"run_changed"))
        with self.assertRaises(ContractError):
            self.journal.append("stimulus_finished", 0)

    def test_constructor_recovery_is_explicit_and_alias_is_idempotent(self):
        self.journal.append("run_started")
        with self.journal.path.open("ab") as target:
            target.write(b'{"kind":')
        with self.assertRaises(ContractError):
            Journal(self.root, ATTEMPT, 2)
        recovered = Journal(self.root, ATTEMPT, 2, recover_incomplete_tail=True)
        self.assertEqual(len(recovered.read_events()), 1)
        self.assertEqual(recovered.recover_tail(), 0)

    def test_complete_non_strict_json_tail_is_corruption_not_recoverable(self):
        self.journal.append("run_started")
        prefix = self.journal.path.read_bytes()
        for tail in (b'{"kind": "a", "kind": "b"}', b'{"time":NaN}'):
            self.journal.path.write_bytes(prefix + tail)
            with self.assertRaises(ContractError):
                self.journal.recover_incomplete_tail()
            self.assertEqual(self.journal.path.read_bytes(), prefix + tail)

    def test_resealed_events_still_require_exact_schema_chain_and_transitions(self):
        self.journal.append("run_started")
        first = self.journal.read_events()[0]
        valid = self.journal.path.read_bytes()
        base = {"schema_version": "dbp-pgl-event-v1", "attempt_id": ATTEMPT,
                "sequence": 2, "previous_sha256": first["event_sha256"],
                "kind": "trial_loaded", "trial_index": 0, "payload": {}}
        for changes in ({"kind": "stimulus_finished"}, {"trial_index": True},
                        {"trial_index": 1}, {"sequence": True}, {"sequence": 3},
                        {"previous_sha256": "0" * 64}, {"attempt_id": "b" * 32},
                        {"schema_version": "future"}, {"wall_time": 42}, {"payload": []}):
            event = seal({**base, **changes}, "event_sha256")
            line = json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            self.journal.path.write_bytes(valid + line)
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.journal.read_events()

    def test_partial_write_failure_blocks_until_explicit_recovery(self):
        self.journal.append("run_started")
        self.journal.append("trial_loaded", 0)
        self.journal.append("stimulus_started", 0)
        write = os.write
        writes = 0

        def interrupted_write(descriptor, content):
            nonlocal writes
            writes += 1
            if writes == 1:
                return write(descriptor, content[:20])
            raise OSError("simulated process loss")

        with patch("dbp_pgl_runner.journal.os.write", side_effect=interrupted_write):
            with self.assertRaises(OSError):
                self.journal.append("stimulus_finished", 0)
        with self.assertRaises(ContractError):
            self.journal.append("stimulus_finished", 0)
        self.assertEqual(self.journal.recover_tail(), 20)
        status = self.journal.status()
        self.assertTrue(status["exposure_incomplete"])
        self.assertFalse(status["can_resume"])
        self.assertEqual(status["last_sequence"], 3)
        self.assertEqual(status["last_sha256"], self.journal.read_events()[-1]["event_sha256"])

    def test_concurrent_identical_append_has_one_durable_event(self):
        def append_start(_):
            return Journal(self.root, ATTEMPT, 2).append("run_started")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(append_start, range(8)))
        self.assertEqual(len(self.journal.read_events()), 1)
        self.assertTrue(all(event == results[0] for event in results))
