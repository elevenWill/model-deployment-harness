from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._common import HarnessError
from scripts.deployment_archive import DeploymentArchive


def test_record_appends_hashed_artifacts_and_preserves_history(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text("{}\n", encoding="utf-8")
    archive = DeploymentArchive("dep-1", root=tmp_path / "deployments")

    archive.record(
        stage="INTAKE",
        status="PASS",
        summary="已保存完整需求",
        artifacts=[request_path],
        occurred_at="2026-08-17T00:00:00Z",
    )
    archive.record(
        stage="HOST_DISCOVERY",
        status="PASS",
        summary="只读主机检查完成",
        occurred_at="2026-08-17T00:01:00Z",
    )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert document["deployment_id"] == "dep-1"
    assert [event["sequence"] for event in document["events"]] == [1, 2]
    assert [event["stage"] for event in document["events"]] == [
        "INTAKE",
        "HOST_DISCOVERY",
    ]
    assert document["events"][0]["artifacts"] == [
        {
            "path": str(request_path),
            "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
            "media_type": "application/json",
        }
    ]


def test_failed_execution_creates_step_record_and_incident(tmp_path: Path) -> None:
    archive = DeploymentArchive(
        "dep-failed",
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    archive.record(
        stage="EXECUTE",
        status="BLOCKED",
        summary="模型下载失败",
        host_id="host-1",
        occurred_at="2026-08-17T01:00:00Z",
        details={
            "plan_sha256": "a" * 64,
            "started_at": "2026-08-17T00:59:00Z",
            "completed_at": "2026-08-17T01:00:00Z",
            "steps": [
                {
                    "step_id": "download-model",
                    "started_at": "2026-08-17T00:59:10Z",
                    "completed_at": "2026-08-17T01:00:00Z",
                    "returncode": 7,
                    "stdout": "download started",
                    "stderr": "network unavailable",
                }
            ],
            "blocker": "步骤 download-model 以退出状态 7 失败",
        },
    )

    execution_path = archive.directory / "execution-0001.json"
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    assert execution["status"] == "BLOCKED"
    assert execution["steps"][0]["stderr_redacted"] == "network unavailable"

    incident_path = tmp_path / "knowledge" / "incidents" / "incident-dep-failed-0001.json"
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    assert incident["deployment_id"] == "dep-failed"
    assert incident["status"] == "OPEN"
    assert incident["symptom"] == "步骤 download-model 以退出状态 7 失败"

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    artifact_paths = {item["path"] for item in document["events"][0]["artifacts"]}
    assert str(execution_path) in artifact_paths
    assert str(incident_path) in artifact_paths


def test_verified_event_updates_deployment_and_creates_benchmark(tmp_path: Path) -> None:
    archive = DeploymentArchive(
        "dep-verified",
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    deployment = {
        "request_ref": "request:req-1",
        "plan_ref": "sha256:" + "a" * 64,
        "model": {"id": "minimax-h3", "variant": "both", "path": "/models/H3"},
        "framework": {"name": "vllm-omni", "version": "abc1234"},
        "target": {
            "gpu_ids": [1],
            "install_root": "/srv/H3",
            "bind_host": "127.0.0.1",
            "port": 8091,
        },
    }
    archive.record(
        stage="EXECUTE",
        status="EXECUTED",
        summary="执行完成",
        host_id="host-1",
        occurred_at="2026-08-17T01:00:00Z",
        details={
            "plan_sha256": "a" * 64,
            "started_at": "2026-08-17T00:59:00Z",
            "completed_at": "2026-08-17T01:00:00Z",
            "steps": [],
            "deployment": deployment,
        },
    )
    verification_path = tmp_path / "verification.json"
    verification_path.write_text("{}\n", encoding="utf-8")
    archive.record(
        stage="VERIFY",
        status="VERIFIED",
        summary="L1 至 L6 验证通过",
        host_id="host-1",
        artifacts=[verification_path],
        occurred_at="2026-08-17T01:10:00Z",
        details={
            "verification_ref": str(verification_path),
            "workload": {"recipe_ref": "models/minimax-h3/verify.yaml"},
            "environment": {
                "framework": "vllm-omni",
                "gpu_topology": "1xGPU",
            },
            "metrics": {"generation_duration_seconds": 120.5, "peak_vram_bytes": None},
        },
    )

    deployment_record = json.loads(
        (tmp_path / "deployments" / "dep-verified.json").read_text(encoding="utf-8")
    )
    assert deployment_record["deployment_status"] == "VERIFIED"
    assert deployment_record["known_state"]["verification_ref"] == str(verification_path)
    assert deployment_record["benchmark_refs"] == ["benchmark-dep-verified-0002"]

    benchmark = json.loads(
        (
            tmp_path
            / "knowledge"
            / "benchmarks"
            / "benchmark-dep-verified-0002.json"
        ).read_text(encoding="utf-8")
    )
    assert benchmark["metrics"] == {"generation_duration_seconds": 120.5}
    assert benchmark["verification_ref"] == str(verification_path)
    archive_document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert [event["stage"] for event in archive_document["events"]] == [
        "EXECUTE",
        "VERIFY",
        "RECORD",
    ]
    assert archive_document["events"][-1]["status"] == "RECORDED"


def test_archive_rejects_secret_bearing_details(tmp_path: Path) -> None:
    archive = DeploymentArchive("dep-secret", root=tmp_path / "deployments")

    with pytest.raises(HarnessError, match="密钥字段"):
        archive.record(
            stage="INTAKE",
            status="DRAFT",
            summary="不应落盘",
            details={"password": "never-store-this"},
        )

    assert not archive.path.exists()


def test_blocked_verification_attempt_is_kept_without_forging_result(tmp_path: Path) -> None:
    archive = DeploymentArchive("dep-verify-blocked", root=tmp_path / "deployments")

    archive.record(
        stage="VERIFY",
        status="BLOCKED",
        summary="验证输入缺失",
        host_id="host-1",
        details={"error_type": "HarnessError"},
    )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert document["events"][0]["status"] == "BLOCKED"
    assert not (tmp_path / "deployments" / "dep-verify-blocked.json").exists()
