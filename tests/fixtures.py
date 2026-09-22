import hashlib
import json

from dbp_pgl_runner.compatibility import expected_compatibility


MEDIA = b"integration-video-bytes"
TOKEN = "test-device-secret"


def seal(value, field="package_sha256"):
    copied = json.loads(json.dumps(value))
    copied.pop(field, None)
    encoded = json.dumps(copied, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    copied[field] = hashlib.sha256(encoded).hexdigest()
    return copied


def block():
    return seal({
        "schema_version": "dbp-pgl-block-v1", "mode": "integration_test",
        "pgl_ready": False, "compatibility": expected_compatibility(),
        "package_id": "a" * 32,
        "experiment_id": "b" * 32, "subject_id": "subject-001",
        "block_id": "c" * 32,
        "trials": [{
            "trial_index": index, "clip_id": "clip one", "role": role,
            "condition": condition, "media_path": "media/clip.mp4",
            "media_bytes": len(MEDIA), "media_sha256": hashlib.sha256(MEDIA).hexdigest(),
        } for index, (role, condition) in enumerate([
            ("parent", "new-integration-parent"), ("repeat", "old-integration-repeat"),
        ])],
    })


def device():
    return {"device_id": "d" * 32, "experiment_id": "b" * 32,
            "owner_id": "e" * 32, "device_name": "test workstation", "created_at": 1,
            "token": TOKEN}
