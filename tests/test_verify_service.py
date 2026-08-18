from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._common import HarnessError, file_sha256
from scripts.run_inference import resolve_output_binding
from scripts.verify_service import (
    _validate_inference_proof,
    _validate_semantic_review,
    _verified_artifact,
    evaluate_overall,
)


def _levels(status: str = "PASS") -> dict:
    return {
        name: {"status": status, "checked_at": "2026-08-17T00:00:00Z", "detail": "fixture"}
        for name in (
            "L1_environment",
            "L2_process",
            "L3_port",
            "L4_api",
            "L5_real_inference",
            "L6_output_validation",
        )
    }


def _with_download(binding: dict, output: Path) -> dict:
    return binding | {
        "downloaded_at": "2026-08-17T00:01:01Z",
        "download_sha256": file_sha256(output),
        "download_content_length": str(output.stat().st_size),
        "response_headers": {"content_length": str(output.stat().st_size)},
    }


def test_started_service_with_failed_inference_is_not_verified() -> None:
    levels = _levels()
    levels["L5_real_inference"]["status"] = "FAIL"
    assert evaluate_overall(levels, 20.0, True) == "FAILED"


def test_l5_l6_need_duration_and_artifact() -> None:
    levels = _levels()
    assert evaluate_overall(levels, None, True) == "INCOMPLETE"
    assert evaluate_overall(levels, 20.0, False) == "INCOMPLETE"
    assert evaluate_overall(levels, 20.0, True) == "VERIFIED"


def test_nonexistent_or_wrong_hash_artifact_is_not_verified(tmp_path: Path) -> None:
    missing = {"path": str(tmp_path / "missing.mp4"), "sha256": "0" * 64}
    assert _verified_artifact(missing) is False
    existing = tmp_path / "output.mp4"
    existing.write_bytes(b"not-a-real-video")
    assert _verified_artifact({"path": str(existing), "sha256": "0" * 64}) is False


def test_untyped_claim_cannot_be_l5_evidence(tmp_path: Path) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(b"not-a-real-video")
    plan = {
        "deployment_id": "dep-1",
        "review": {"plan_sha256": "a" * 64},
        "service": {"bind_host": "127.0.0.1", "port": 30011},
    }
    with pytest.raises(HarnessError, match="模式校验失败"):
        _validate_inference_proof({"claimed": "success"}, plan, output)


def test_inference_proof_binds_request_response_endpoint_and_output(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    output = tmp_path / "output.mp4"
    request.write_text('{"prompt":"fixture"}')
    output.write_bytes(b"fixture-output")
    response.write_text(
        '{"id":"job-1","status":"completed","output":'
        f'{{"id":"artifact-1","url":"/outputs/artifact-1","sha256":"{file_sha256(output)}"}}}}'
    )
    artifact = lambda path, media: {  # noqa: E731 - compact fixture builder
        "path": str(path), "sha256": file_sha256(path), "media_type": media
    }
    plan = {
        "deployment_id": "dep-1",
        "review": {"plan_sha256": "a" * 64},
        "service": {"bind_host": "0.0.0.0", "port": 30011},
    }
    recipe = {
        "inference_api": {
            "submit_paths": ["/v1/videos"],
            "output_reference": {
                "resolver": "json_pointer_fields",
                "artifact_id_pointer": "/output/id",
                "url_pointer": "/output/url",
                "content_sha256_pointer": "/output/sha256",
            },
        }
    }
    raw_response = json.loads(response.read_text())
    proof = {
        "schema_version": "1.0",
        "producer": "HARNESS_HTTP_RUNNER",
        "deployment_id": "dep-1",
        "plan_sha256": "a" * 64,
        "endpoint": "http://127.0.0.1:30011",
        "request": {
            "method": "POST", "path": "/v1/videos",
            "payload": artifact(request, "application/json"),
            "submitted_at": "2026-08-17T00:00:00Z",
        },
        "job": {
            "job_id": "job-1", "status": "COMPLETED",
            "completed_at": "2026-08-17T00:01:00Z",
            "response": artifact(response, "application/json"), "runtime_error": None,
        },
        "output_binding": _with_download(
            resolve_output_binding(
                raw_response,
                "job-1",
                recipe["inference_api"],
                "http://127.0.0.1:30011/",
            ),
            output,
        ),
        "output": artifact(output, "video/mp4"),
    }
    duration = _validate_inference_proof(proof, plan, output, recipe)
    assert duration == 60
    proof["endpoint"] = "http://127.0.0.1:39999"
    with pytest.raises(HarnessError, match="端口"):
        _validate_inference_proof(proof, plan, output, recipe)


def test_completed_response_from_another_output_cannot_validate_old_media(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    output = tmp_path / "old.mp4"
    request.write_text('{"prompt":"fixture"}')
    output.write_bytes(b"old-output")
    response.write_text(json.dumps({
        "id": "job-2", "status": "COMPLETED",
        "output": {"id": "new-output", "url": "/outputs/new", "sha256": "0" * 64},
    }))
    artifact = lambda path, media: {  # noqa: E731
        "path": str(path), "sha256": file_sha256(path), "media_type": media
    }
    recipe = {"inference_api": {
        "submit_paths": ["/v1/videos"],
        "output_reference": {
            "resolver": "json_pointer_fields", "artifact_id_pointer": "/output/id",
            "url_pointer": "/output/url", "content_sha256_pointer": "/output/sha256",
        },
    }}
    proof = {
        "schema_version": "1.0", "producer": "HARNESS_HTTP_RUNNER",
        "deployment_id": "dep-1", "plan_sha256": "a" * 64,
        "endpoint": "http://127.0.0.1:30011",
        "request": {"method": "POST", "path": "/v1/videos",
                    "payload": artifact(request, "application/json"),
                    "submitted_at": "2026-08-17T00:00:00Z"},
        "job": {"job_id": "job-2", "status": "COMPLETED",
                "completed_at": "2026-08-17T00:01:00Z",
                "response": artifact(response, "application/json"), "runtime_error": None},
        "output_binding": _with_download(
            resolve_output_binding(
                json.loads(response.read_text()),
                "job-2",
                recipe["inference_api"],
                "http://127.0.0.1:30011/",
            ),
            output,
        ),
        "output": artifact(output, "video/mp4"),
    }
    plan = {"deployment_id": "dep-1", "review": {"plan_sha256": "a" * 64},
            "service": {"bind_host": "0.0.0.0", "port": 30011}}
    with pytest.raises(HarnessError, match="原始完成响应声明"):
        _validate_inference_proof(proof, plan, output, recipe)


def test_semantic_review_is_bound_to_output_and_reviewer(tmp_path: Path) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(b"fixture-output")
    plan = {"deployment_id": "dep-1", "review": {"plan_sha256": "a" * 64}}
    review = {
        "schema_version": "1.0", "review_id": "review-1", "deployment_id": "dep-1",
        "plan_sha256": "a" * 64, "output_sha256": file_sha256(output),
        "reviewed_by": "human@example.test", "reviewed_at": "2026-08-17T00:02:00Z",
        "checks": {
            "not_blank_or_frozen": "PASS", "audio_present_not_silent": "PASS",
            "task_alignment": "PASS",
        },
    }
    _validate_semantic_review(review, plan, output)
    review["output_sha256"] = "0" * 64
    with pytest.raises(HarnessError, match="生成输出"):
        _validate_semantic_review(review, plan, output)
