import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dbp_api import Client, Experiment, Assignments, Session, Trial, Image, UnsupportedMediaError
from tests.test_client_sdk import manifest


class WorkflowTests(unittest.TestCase):
    def client(self):
        client = Client("http://localhost:8773")
        client.query_media = Mock(return_value=dict(version="dataset", search_version=None))
        client.subject_manifest = Mock(return_value=manifest())
        self.states = {}
        self.events = {}

        def request(method, path, body=None):
            if path.endswith("/progress"):
                return dict(manifest_sha256=manifest()["manifest_sha256"], trials=self.states.copy())
            if path.endswith("/events"):
                for event in body["events"]:
                    self.events[event["event_id"]] = event
                    self.states[event["trial_id"]] = dict(status=event["kind"], attempt_id=event["attempt_id"])
                return dict(accepted=[event["event_id"] for event in body["events"]],
                            manifest_sha256=manifest()["manifest_sha256"])
            raise AssertionError(path)

        client._json = Mock(side_effect=request)
        return client

    def test_experiment_configuration_and_assignments(self):
        client = self.client()
        client._create_experiment = Mock(return_value={"id": "experiment-1"})
        client.publish = Mock(return_value={"experiment_id": "experiment-1", "subjects": [manifest()]})
        experiment = client.create_experiment(name="Demo", seed="custom-seed")
        self.assertIsInstance(experiment, Experiment)
        assignments = experiment.assign(subjects=1, items_per_subject=2)
        self.assertIsInstance(assignments, Assignments)
        self.assertEqual(assignments.subject_ids, ("subject-001",))
        spec = client._create_experiment.call_args.args[0]
        self.assertEqual(spec.seed, "custom-seed")
        self.assertEqual(client._create_experiment.call_args.kwargs["version"], "dataset")
        with self.assertRaises(UnsupportedMediaError):
            client.create_experiment(name="Images", seed="1", media_type=Image)

    def test_local_journal_and_remote_resume(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            session = Session(client, "experiment-1", "subject-001", workspace=workspace)
            trial = session.trials[0]
            self.assertIsInstance(trial, Trial)
            with self.assertRaisesRegex(ValueError, "download"):
                session.started(trial)
            media = workspace / "video.mp4"
            media.write_bytes(b"example")
            local_manifest = workspace / "manifest.json"
            local_manifest.write_text(json.dumps({"manifest": manifest(), "files": {item.trial_id: media.name for item in session.trials}}))
            client.download_subject = Mock(return_value=local_manifest)
            session.download(workspace / "download")
            trial = session.trials[0]
            session.started(trial)
            self.assertEqual(len(session.incomplete_trials), 1)
            self.assertEqual(len(session.pending_trials), 1)
            session.completed(trial)
            session.completed(trial)
            self.assertEqual(len(self.events), 2)
            other = Session(client, "experiment-1", "subject-001", workspace=workspace / "other-device")
            self.assertEqual([item.trial_id for item in other.pending_trials], ["trial-2"])
            self.assertEqual(other.incomplete_trials, ())

    def test_failed_upload_keeps_event_for_retry(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(client, "experiment-1", "subject-001", workspace=Path(temporary))
            session._record("trial-1", "started", "a" * 32)
            original = client._json.side_effect
            client._json.side_effect = ConnectionError("offline")
            with self.assertRaises(ConnectionError):
                session.sync()
            client._json.side_effect = original
            session.sync()
            session.sync()
            self.assertEqual(len(self.events), 1)
            self.assertEqual(len(session.pending_trials), 1)

    def test_lost_acknowledgment_can_be_retried_without_duplicate_events(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as temporary:
            session = Session(client, "experiment-1", "subject-001", workspace=Path(temporary))
            session._record("trial-1", "started", "a" * 32)
            original = client._json.side_effect

            def lost_reply(method, path, body=None):
                original(method, path, body)
                raise ConnectionError("reply lost")

            client._json.side_effect = lost_reply
            with self.assertRaises(ConnectionError):
                session.sync()
            client._json.side_effect = original
            reopened = Session(client, "experiment-1", "subject-001", workspace=Path(temporary))
            reopened.sync()
            self.assertEqual(len(self.events), 1)
            self.assertEqual(len(reopened.incomplete_trials), 1)
            with self.assertRaises(ValueError):
                reopened.completed(reopened.trials[1])

    def test_open_existing_assignments_does_not_publish_or_resample(self):
        client = self.client()
        client.experiment = Mock(return_value=dict(publication=dict(subjects=[manifest()])))
        existing = client.assignments("experiment-1")
        self.assertEqual(existing.subject_ids, ("subject-001",))
        client.query_media.assert_not_called()
