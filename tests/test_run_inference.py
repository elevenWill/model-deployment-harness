from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts._common import ROOT, HarnessError, validate_instance
from scripts.deployment_archive import DeploymentArchive
from scripts.run_inference import run_inference
from tests.test_remote_exec import ready_plan


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.body


def test_runner_submits_polls_downloads_and_generates_proof(tmp_path: Path) -> None:
    calls = []
    responses = iter(
        [
            FakeResponse(b'{"id":"job-1","status":"queued"}'),
            FakeResponse(b'{"id":"job-1","status":"COMPLETED"}'),
            FakeResponse(b"actual-media-bytes"),
        ]
    )

    def opener(request, **kwargs):
        calls.append((request.full_url, request.get_method(), kwargs))
        return next(responses)

    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"prompt": "fixture"}), encoding="utf-8")
    output = tmp_path / "output.mp4"
    response = tmp_path / "response.json"
    proof_path = tmp_path / "proof.json"
    recipe = yaml.safe_load((ROOT / "models/minimax-h3/verify.yaml").read_text())
    proof = run_inference(
        ready_plan(), recipe, "http://127.0.0.1:30011", payload, output, response,
        proof_path, poll_interval=0, opener=opener,
    )
    validate_instance(proof, "inference-proof.schema.json")
    assert output.read_bytes() == b"actual-media-bytes"
    assert [item[1] for item in calls] == ["POST", "GET", "GET"]
    assert calls[0][0].endswith("/v1/videos")
    assert calls[2][0].endswith("/v1/videos/job-1/content")


def test_successful_inference_is_automatically_added_to_archive(tmp_path: Path) -> None:
    responses = iter(
        [
            FakeResponse(b'{"id":"job-1","status":"COMPLETED"}'),
            FakeResponse(b"actual-media-bytes"),
        ]
    )
    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"prompt": "fixture"}), encoding="utf-8")
    output = tmp_path / "output.mp4"
    response = tmp_path / "response.json"
    proof_path = tmp_path / "proof.json"
    plan = ready_plan()
    archive = DeploymentArchive(plan["deployment_id"], root=tmp_path / "deployments")

    run_inference(
        plan,
        yaml.safe_load((ROOT / "models/minimax-h3/verify.yaml").read_text()),
        "http://127.0.0.1:30011",
        payload,
        output,
        response,
        proof_path,
        poll_interval=0,
        opener=lambda *_args, **_kwargs: next(responses),
        archive=archive,
    )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert document["events"][0]["stage"] == "INFERENCE"
    assert document["events"][0]["status"] == "PASS"
    assert {Path(item["path"]).name for item in document["events"][0]["artifacts"]} == {
        "request.json",
        "response.json",
        "output.mp4",
        "proof.json",
    }


def test_failed_inference_attempt_is_automatically_added_to_archive(tmp_path: Path) -> None:
    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"prompt": "fixture"}), encoding="utf-8")
    plan = ready_plan()
    archive = DeploymentArchive(plan["deployment_id"], root=tmp_path / "deployments")

    with pytest.raises(HarnessError, match="任务失败"):
        run_inference(
            plan,
            yaml.safe_load((ROOT / "models/minimax-h3/verify.yaml").read_text()),
            "http://127.0.0.1:30011",
            payload,
            tmp_path / "output.mp4",
            tmp_path / "response.json",
            tmp_path / "proof.json",
            poll_interval=0,
            opener=lambda *_args, **_kwargs: FakeResponse(
                b'{"id":"job-1","status":"FAILED"}'
            ),
            archive=archive,
        )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert (document["events"][0]["stage"], document["events"][0]["status"]) == (
        "INFERENCE",
        "BLOCKED",
    )


def test_runner_rejects_unreviewed_recipe_before_http(tmp_path: Path) -> None:
    payload = tmp_path / "request.json"
    payload.write_text('{"prompt":"fixture"}', encoding="utf-8")
    fake_recipe = {
        "model_id": "minimax-h3",
        "inference_api": {
            "submit_path": "/admin/delete",
            "status_path_template": "/jobs/{id}",
            "content_path_template": "/jobs/{id}/content",
        },
    }
    with pytest.raises(HarnessError, match="已审核配方"):
        run_inference(
            ready_plan(), fake_recipe, "http://127.0.0.1:30011", payload,
            tmp_path / "out.mp4", tmp_path / "response.json", tmp_path / "proof.json",
            opener=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
        )
