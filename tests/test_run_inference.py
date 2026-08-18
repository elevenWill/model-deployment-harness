from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from scripts._common import ROOT, HarnessError, validate_instance
from scripts.deployment_archive import DeploymentArchive
from scripts.run_inference import generated_request_path, resolve_output_binding, run_inference
from tests.test_remote_exec import ready_comfyui_plan, ready_plan


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.body


def test_runner_submits_polls_downloads_and_generates_proof(tmp_path: Path) -> None:
    calls = []
    media_hash = hashlib.sha256(b"actual-media-bytes").hexdigest()
    responses = iter(
        [
            FakeResponse(b'{"id":"job-1","status":"queued"}'),
            FakeResponse(json.dumps({
                "id": "job-1", "status": "COMPLETED",
                "output": {
                    "id": "artifact-1",
                    "url": "/artifacts/artifact-1",
                    "sha256": media_hash,
                },
            }).encode()),
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
        ready_plan(),
        recipe,
        "http://127.0.0.1:30011",
        payload,
        output,
        response,
        proof_path,
        poll_interval=0,
        opener=opener,
    )
    validate_instance(proof, "inference-proof.schema.json")
    assert output.read_bytes() == b"actual-media-bytes"
    assert [item[1] for item in calls] == ["POST", "GET", "GET"]
    assert calls[0][0].endswith("/v1/videos")
    assert calls[2][0].endswith("/artifacts/artifact-1")
    assert proof["output_binding"]["response_artifact_pointer"] == "/output/id"
    assert proof["output_binding"]["artifact_id"] == "artifact-1"


def test_runner_supports_comfyui_prompt_history_and_mp4_view(tmp_path: Path) -> None:
    calls = []
    job_token = ""

    def opener(request, **kwargs):
        nonlocal job_token
        calls.append((request.full_url, request.get_method(), kwargs))
        if request.get_method() == "POST":
            submitted = json.loads(request.data)
            job_token = submitted["prompt"]["42"]["inputs"]["filename_prefix"]
            return FakeResponse(b'{"prompt_id":"prompt-1","number":1}')
        if "/history/" in request.full_url:
            return FakeResponse(json.dumps({
                "prompt-1": {"outputs": {"42": {"gifs": [{
                    "filename": job_token + ".mp4", "subfolder": "", "type": "output",
                }]}}},
            }).encode())
        return FakeResponse(
            b"actual-comfy-media", {"Content-Length": str(len(b"actual-comfy-media"))}
        )

    payload = tmp_path / "workflow.json"
    payload.write_text(json.dumps({
        "prompt": {"42": {"class_type": "SaveVideo", "inputs": {"filename_prefix": ""}}}
    }))
    original_payload = payload.read_bytes()
    output = tmp_path / "output.mp4"
    response = tmp_path / "history.json"
    proof_path = tmp_path / "proof.json"
    recipe = yaml.safe_load((ROOT / "models/minimax-h3/verify-comfyui.yaml").read_text())
    proof = run_inference(
        ready_comfyui_plan(),
        recipe,
        "http://127.0.0.1:8188",
        payload,
        output,
        response,
        proof_path,
        poll_interval=0,
        opener=opener,
    )
    assert proof["job"]["job_id"] == "prompt-1"
    assert payload.read_bytes() == original_payload
    submitted_request = generated_request_path(proof_path)
    assert proof["request"]["payload"]["path"] == str(submitted_request)
    assert json.loads(submitted_request.read_bytes())["prompt"]["42"]["inputs"][
        "filename_prefix"
    ] == job_token
    assert [item[1] for item in calls] == ["POST", "GET", "GET"]
    assert calls[0][0].endswith("/prompt")
    assert calls[1][0].endswith("/history/prompt-1")
    assert calls[2][0].endswith(
        f"/view?filename={job_token}.mp4&subfolder=&type=output"
    )
    assert proof["output_binding"]["job_token"] == job_token
    assert proof["output_binding"]["output_node_id"] == "42"
    assert proof["output_binding"]["output_collection"] == "gifs"
    assert proof["output_binding"]["output_index"] == 0
    assert proof["output_binding"]["download_content_length"] == str(
        len(b"actual-comfy-media")
    )


def test_comfyui_rejects_old_same_path_and_different_prompt_or_output() -> None:
    api = yaml.safe_load(
        (ROOT / "models/minimax-h3/verify-comfyui.yaml").read_text()
    )["inference_api"]
    current_token = "harness-h3-" + "a" * 32
    request = {"prompt": {"42": {"inputs": {"filename_prefix": current_token}}}}
    old_history = {
        "id": "prompt-new",
        "status": "COMPLETED",
        "history": {"prompt-old": {"outputs": {"42": {"gifs": [{
            "filename": "harness-h3-" + "b" * 32 + ".mp4",
            "subfolder": "", "type": "output",
        }]}}}},
    }
    with pytest.raises(HarnessError, match="history"):
        resolve_output_binding(
            old_history, "prompt-new", api, "http://127.0.0.1:8188/", request
        )

    different_output = {
        "id": "prompt-new", "status": "COMPLETED",
        "history": {"prompt-new": {"outputs": {"42": {"gifs": [{
            "filename": "harness-h3-" + "b" * 32 + ".mp4",
            "subfolder": "", "type": "output",
        }]}}}},
    }
    with pytest.raises(HarnessError, match="唯一 job token"):
        resolve_output_binding(
            different_output, "prompt-new", api, "http://127.0.0.1:8188/", request
        )

    ambiguous_outputs = json.loads(json.dumps(different_output))
    ambiguous_outputs["history"]["prompt-new"]["outputs"]["42"]["gifs"] = [
        {
            "filename": current_token + ".mp4",
            "subfolder": "",
            "type": "output",
        },
        {
            "filename": current_token + "-other.mp4",
            "subfolder": "",
            "type": "output",
        },
    ]
    with pytest.raises(HarnessError, match="恰好产出一个"):
        resolve_output_binding(
            ambiguous_outputs,
            "prompt-new",
            api,
            "http://127.0.0.1:8188/",
            request,
        )


def test_successful_inference_is_automatically_added_to_archive(tmp_path: Path) -> None:
    media_hash = hashlib.sha256(b"actual-media-bytes").hexdigest()
    responses = iter(
        [
            FakeResponse(json.dumps({
                "id": "job-1", "status": "COMPLETED",
                "output": {
                    "id": "artifact-1",
                    "url": "/artifacts/artifact-1",
                    "sha256": media_hash,
                },
            }).encode()),
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
        "proof.generated-request.json",
        "response.json",
        "output.mp4",
        "proof.json",
    }


def test_runner_rejects_download_not_matching_completed_response(tmp_path: Path) -> None:
    responses = iter([
        FakeResponse(json.dumps({
            "id": "job-1", "status": "COMPLETED",
            "output": {"id": "new-output", "url": "/outputs/new", "sha256": "0" * 64},
        }).encode()),
        FakeResponse(b"old-mp4-from-another-job"),
    ])
    payload = tmp_path / "request.json"
    payload.write_text('{"prompt":"fixture"}', encoding="utf-8")

    with pytest.raises(HarnessError, match="完成响应声明"):
        run_inference(
            ready_plan(),
            yaml.safe_load((ROOT / "models/minimax-h3/verify.yaml").read_text()),
            "http://127.0.0.1:30011",
            payload,
            tmp_path / "output.mp4",
            tmp_path / "response.json",
            tmp_path / "proof.json",
            poll_interval=0,
            opener=lambda *_args, **_kwargs: next(responses),
        )
    assert not (tmp_path / "output.mp4").exists()


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
            opener=lambda *_args, **_kwargs: FakeResponse(b'{"id":"job-1","status":"FAILED"}'),
            archive=archive,
        )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert (document["events"][0]["stage"], document["events"][0]["status"]) == (
        "INFERENCE",
        "BLOCKED",
    )


def test_comfyui_http_failure_preserves_original_payload_and_keeps_submitted_evidence(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "workflow.json"
    payload.write_text(json.dumps({
        "prompt": {"42": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "user"}}}
    }))
    original_payload = payload.read_bytes()
    proof_path = tmp_path / "proof.json"
    submitted_request = generated_request_path(proof_path)

    with pytest.raises(urllib.error.URLError):
        run_inference(
            ready_comfyui_plan(),
            yaml.safe_load((ROOT / "models/minimax-h3/verify-comfyui.yaml").read_text()),
            "http://127.0.0.1:8188",
            payload,
            tmp_path / "output.mp4",
            tmp_path / "response.json",
            proof_path,
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.URLError("fixture HTTP failure")
            ),
        )

    assert payload.read_bytes() == original_payload
    assert submitted_request.is_file()
    submitted = json.loads(submitted_request.read_bytes())
    assert submitted["prompt"]["42"]["inputs"]["filename_prefix"].startswith("harness-h3-")


def test_generated_request_artifact_is_never_overwritten(tmp_path: Path) -> None:
    payload = tmp_path / "request.json"
    payload.write_text('{"prompt":"fixture"}', encoding="utf-8")
    original_payload = payload.read_bytes()
    proof_path = tmp_path / "proof.json"
    submitted_request = generated_request_path(proof_path)
    submitted_request.write_bytes(b"user-owned")

    with pytest.raises(HarnessError, match="不得已存在"):
        run_inference(
            ready_plan(),
            yaml.safe_load((ROOT / "models/minimax-h3/verify.yaml").read_text()),
            "http://127.0.0.1:30011",
            payload,
            tmp_path / "output.mp4",
            tmp_path / "response.json",
            proof_path,
            opener=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
        )

    assert submitted_request.read_bytes() == b"user-owned"
    assert payload.read_bytes() == original_payload


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
            ready_plan(),
            fake_recipe,
            "http://127.0.0.1:30011",
            payload,
            tmp_path / "out.mp4",
            tmp_path / "response.json",
            tmp_path / "proof.json",
            opener=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
        )
