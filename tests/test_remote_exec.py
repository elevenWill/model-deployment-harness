from __future__ import annotations

import json

import pytest

from scripts._common import ROOT, canonical_plan_sha256, file_sha256
from scripts.deployment_archive import DeploymentArchive
from scripts.probe_host import CommandResult
from scripts.remote_exec import (
    ExecutionBlocked,
    archive_reviewed_lifecycle,
    execute_plan,
    validate_executable_plan,
)


def ready_plan():
    step = {
        "step_id": "mkdir-install",
        "sequence": 1,
        "name": "create root",
        "action": "create_target_directory",
        "action_class": "PLAN_ALLOWED_WRITE",
        "command": ["mkdir", "-p", "/opt/models/example"],
        "success_criteria": ["directory exists"],
        "rollback_step_ids": ["inspect-rollback"],
    }
    rollback = {
        "step_id": "inspect-rollback",
        "sequence": 1,
        "name": "inspect",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["test", "-d", "/opt/models/example"],
        "success_criteria": ["state observed"],
        "rollback_step_ids": [],
    }
    lifecycle_files = {
        "INTAKE": "tests/fixtures/lifecycle/intake.json",
        "REQUIREMENT_GATE": "tests/fixtures/lifecycle/requirement-gate.json",
        "HOST_DISCOVERY": "tests/fixtures/lifecycle/host-discovery.json",
        "RESEARCH": "tests/fixtures/lifecycle/research.json",
        "PLAN": "tests/fixtures/lifecycle/plan.json",
        "PLAN_REVIEW": "tests/fixtures/lifecycle/plan-review.json",
    }
    plan = {
        "schema_version": "1.0",
        "deployment_id": "dep-1",
        "request_id": "req-1",
        "created_at": "2026-08-17T00:00:00Z",
        "host_profile_observed_at": "2026-08-17T00:01:00Z",
        "target": {
            "host_id": "knode25",
            "gpu_ids": [0],
            "install_root": "/opt/models",
            "model_root": "/models",
        },
        "model": {
            "id": "minimax-h3",
            "variant": "fl2va",
            "recipe_ref": "models/minimax-h3/manifest.yaml",
        },
        "framework": {
            "name": "sglang",
            "version": "a54de989c8ba817ebb603c5443e694e5fcf7edb1",
            "recipe_ref": "models/minimax-h3/recipes/sglang.yaml",
            "runtime_artifact": {
                "kind": "source_checkout",
                "location": "/opt/models/sglang",
                "revision": "a54de989c8ba817ebb603c5443e694e5fcf7edb1",
                "probe_command": [
                    "git",
                    "-C",
                    "/opt/models/sglang",
                    "rev-parse",
                    "HEAD",
                ],
                "executable": "/opt/models/sglang/.venv/bin/sglang",
            },
            "rationale": "official support",
            "evidence_ids": ["ev-1"],
        },
        "compatibility": {"profile_id": "fixture-compatible", "required_cuda": "12.6"},
        "environment": {"strategy": "container", "isolated": True, "rationale": "preserve host"},
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "lifecycle": {
            "transitions": [
                {
                    "stage": stage,
                    "completed_at": "2026-08-17T00:00:00Z",
                    "artifact": {
                        "path": lifecycle_files[stage],
                        "sha256": file_sha256(ROOT / lifecycle_files[stage]),
                    },
                }
                for stage in (
                    "INTAKE",
                    "REQUIREMENT_GATE",
                    "HOST_DISCOVERY",
                    "RESEARCH",
                    "PLAN",
                    "PLAN_REVIEW",
                )
            ]
        },
        "executor_controls": {
            "remote_writer_lock": {
                "path": "/tmp/model-deployment-harness-writer-lock",
                "acquire_command": ["mkdir", "/tmp/model-deployment-harness-writer-lock"],
                "release_command": ["rmdir", "/tmp/model-deployment-harness-writer-lock"],
            }
        },
        "steps": [step],
        "risks": [],
        "required_changes": [
            {
                "description": "create isolated root",
                "action_class": "PLAN_ALLOWED_WRITE",
                "step_ids": ["mkdir-install"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "ev-1",
                "source": {
                    "title": "Official docs",
                    "url": "https://example.com/docs",
                    "authority_tier": "A",
                    "publisher": "Framework",
                },
                "retrieved_at": "2026-08-17T00:00:00Z",
                "claim": "supported",
                "applies_to": ["example"],
                "confidence": "HIGH",
                "officially_verified": True,
                "inference": False,
            }
        ],
        "verification": {
            "recipe_ref": "models/minimax-h3/verify.yaml",
            "required_levels": ["L5_real_inference", "L6_output_validation"],
            "success_condition": "L5_AND_L6_PASS",
        },
        "rollback": {
            "trigger_conditions": ["step failure"],
            "steps": [rollback],
            "preserve_evidence": True,
        },
        "license_gate": {
            "status": "PASS",
            "license_type": "MiniMax H3 Community License",
            "source_url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE",
            "checked_at": "2026-08-17T00:00:00Z",
            "deployment_region": "CN",
            "intended_use": "research",
            "findings": ["allowed"],
            "reviewed_by": "license-reviewer",
            "accepted_by": "operator",
            "accepted_at": "2026-08-17T00:01:00Z",
        },
        "review": {
            "status": "APPROVED",
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-17T00:02:00Z",
            "plan_sha256": "0" * 64,
        },
        "status": "READY",
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    return plan


def deployment_request():
    return {
        "schema_version": "1.0",
        "request_id": "req-1",
        "requested_at": "2026-08-17T00:00:00Z",
        "requested_by": "operator",
        "target": {
            "host": {"host_id": "knode25", "ssh_username": "deploy", "ssh_port": 22},
            "gpu_ids": [0],
            "install_root": "/opt/models",
            "model_root": "/models",
        },
        "model": {"id": "minimax-h3", "variant": "fl2va"},
        "framework_preference": "sglang",
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "research",
        "deployment_region": "CN",
    }


def observed_host():
    return {
        "schema_version": "1.0",
        "host_id": "knode25",
        "observed_at": "2026-08-17T00:01:00Z",
        "probe": {"status": "COMPLETE", "transport": "LOCAL_FIXTURE", "errors": []},
        "identity": {"hostname": "knode25", "addresses": ["10.0.0.25"], "aliases": []},
        "hardware": {
            "cpu": {"model": "fixture", "logical_cores": 32, "architecture": "x86_64"},
            "memory_bytes": 512 * 1024**3,
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-fixture",
                    "model": "fixture GPU",
                    "memory_total_bytes": 80 * 1024**3,
                    "memory_free_bytes": 79 * 1024**3,
                }
            ],
            "gpu_topology": [],
        },
        "software": {
            "os": {"name": "Fixture Linux", "version": "1"},
            "kernel": "6.0",
            "nvidia": {"driver_version": "999", "cuda_compatibility": "12.6"},
            "docker": {"installed": True, "version": "fixture", "nvidia_runtime_available": True},
            "python": [{"executable": "python3", "version": "Python 3.11"}],
            "uv_version": None,
            "conda_version": None,
        },
        "storage": {
            "filesystems": [
                {
                    "path": "/",
                    "total_bytes": 1024**4,
                    "available_bytes": 800 * 1024**3,
                    "filesystem_type": "ext4",
                }
            ],
            "mounts": [{"source": "/dev/x", "target": "/", "options": ["rw"]}],
        },
        "network": {"listening_ports": [22], "connectivity_checks": []},
        "runtime": {"gpu_processes": [], "model_services": []},
    }


class FakeTransport:
    def __init__(self, returncode=0, raised=None):
        self.calls = []
        self.returncode = returncode
        self.raised = raised

    def run(self, argv, *, timeout=20, cwd=None, env=None):
        self.calls.append((tuple(argv), cwd, dict(env or {})))
        if self.raised:
            raise self.raised
        if tuple(argv) == (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ):
            return CommandResult(tuple(argv), 0, "a54de989c8ba817ebb603c5443e694e5fcf7edb1\n", "")
        return CommandResult(tuple(argv), self.returncode, "ok", "")

    def close(self):
        pass


def test_ready_hash_validated_and_exact_argv_executed(tmp_path):
    plan = ready_plan()
    transport = FakeTransport()
    result = execute_plan(
        plan,
        transport,
        request=deployment_request(),
        host_profile=observed_host(),
        lock_directory=tmp_path,
        _probe_collector=lambda _: observed_host(),
    )
    assert result.status == "EXECUTED"
    assert [call[0] for call in transport.calls] == [
        (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ),
        ("mkdir", "/tmp/model-deployment-harness-writer-lock"),
        (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ),
        tuple(plan["steps"][0]["command"]),
        ("rmdir", "/tmp/model-deployment-harness-writer-lock"),
    ]


def test_execute_plan_automatically_records_redacted_step_results(tmp_path):
    plan = ready_plan()
    archive = DeploymentArchive(
        plan["deployment_id"],
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    result = execute_plan(
        plan,
        FakeTransport(),
        request=deployment_request(),
        host_profile=observed_host(),
        lock_directory=tmp_path,
        _probe_collector=lambda _: observed_host(),
        archive=archive,
    )

    assert result.status == "EXECUTED"
    execution = archive.directory / "execution-0001.json"
    assert execution.is_file()
    document = json.loads(execution.read_text(encoding="utf-8"))
    assert document["steps"] == [
        {
            "step_id": "mkdir-install",
            "started_at": document["steps"][0]["started_at"],
            "completed_at": document["steps"][0]["completed_at"],
            "returncode": 0,
            "stdout_redacted": "ok",
            "stderr_redacted": "",
        }
    ]
    assert document["steps"][0]["started_at"] <= document["steps"][0]["completed_at"]
    deployment_record_path = tmp_path / "deployments" / "dep-1.json"
    deployment_record = json.loads(
        deployment_record_path.read_text(encoding="utf-8")
    )
    assert deployment_record["deployment_status"] == "STARTED"
    assert deployment_record["known_state"]["expected_service_state"] == "RUNNING"
    assert deployment_record["framework"]["name"] == "sglang"


def test_preflight_exception_is_automatically_recorded_as_failed_deployment(tmp_path):
    plan = ready_plan()
    archive = DeploymentArchive(
        plan["deployment_id"],
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    request = deployment_request()
    request["target"]["gpu_ids"] = [1]
    with pytest.raises(ExecutionBlocked, match="制品不匹配"):
        execute_plan(
            plan,
            FakeTransport(),
            request=request,
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
            archive=archive,
        )

    record = json.loads(
        (tmp_path / "deployments" / "dep-1.json").read_text(encoding="utf-8")
    )
    assert record["deployment_status"] == "FAILED"
    assert record["incident_refs"] == ["incident-dep-1-0001"]


def test_reviewed_plan_archives_all_prior_lifecycle_artifacts(tmp_path):
    plan = ready_plan()
    request_path = tmp_path / "request.json"
    profile_path = tmp_path / "host-profile.json"
    plan_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps(deployment_request()), encoding="utf-8")
    profile_path.write_text(json.dumps(observed_host()), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    archive = DeploymentArchive(plan["deployment_id"], root=tmp_path / "deployments")

    archive_reviewed_lifecycle(
        archive,
        plan,
        request_path=request_path,
        host_profile_path=profile_path,
        plan_path=plan_path,
    )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert [event["stage"] for event in document["events"]] == [
        "INTAKE",
        "REQUIREMENT_GATE",
        "HOST_DISCOVERY",
        "RESEARCH",
        "PLAN",
        "PLAN_REVIEW",
    ]
    assert all(event["status"] == "PASS" for event in document["events"])
    review_paths = {item["path"] for item in document["events"][-1]["artifacts"]}
    assert str(plan_path) in review_paths


def test_hash_detects_any_post_review_change():
    plan = ready_plan()
    plan["steps"][0]["command"].append("changed")
    with pytest.raises(ExecutionBlocked, match="SHA-256"):
        validate_executable_plan(plan)


def test_unknown_action_fails_closed():
    plan = ready_plan()
    plan["steps"][0].update(action="other", action_class="PLAN_ALLOWED_WRITE")
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="未知动作"):
        validate_executable_plan(plan)


def test_protected_action_requires_approval_scoped_to_step_and_action():
    plan = ready_plan()
    plan["steps"][0].update(
        action="change_system_cuda",
        action_class="PROTECTED",
        approval={
            "status": "APPROVED",
            "approved_by": "operator",
            "approved_at": "2026-08-17T00:03:00Z",
            "scope": "change_system_cuda",
        },
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="限定"):
        validate_executable_plan(plan)


def test_even_scoped_protected_action_is_not_automated():
    plan = ready_plan()
    plan["steps"][0].update(
        action="change_system_cuda",
        action_class="PROTECTED",
        approval={
            "status": "APPROVED",
            "approved_by": "operator",
            "approved_at": "2026-08-17T00:03:00Z",
            "scope": "mkdir-install change_system_cuda",
        },
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="绝不自动执行"):
        validate_executable_plan(plan)


def test_artifact_mismatch_and_ssh_failure_stop_before_later_steps(tmp_path):
    plan = ready_plan()
    transport = FakeTransport(raised=ConnectionError("secret details"))
    mismatched = deployment_request()
    mismatched["target"]["gpu_ids"] = [1]
    with pytest.raises(ExecutionBlocked, match="制品不匹配"):
        execute_plan(
            plan,
            transport,
            request=mismatched,
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )
    assert transport.calls == []
    with pytest.raises(ExecutionBlocked, match="框架运行时探测失败") as caught:
        execute_plan(
            plan,
            transport,
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )
    assert "secret details" not in str(caught.value)


def test_secret_value_in_plan_is_rejected_and_output_is_redacted(tmp_path):
    plan = ready_plan()
    plan["steps"][0]["success_criteria"].append("super-secret-token")
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="密钥值"):
        execute_plan(
            plan,
            FakeTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            environ={"HF_TOKEN": "super-secret-token"},
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )


def test_occupied_gpu_cannot_be_bypassed_with_caller_assertion(tmp_path):
    host = observed_host()
    host["runtime"]["gpu_processes"] = [
        {"gpu_id": "GPU-fixture", "pid": 88, "process_name": "unrelated", "memory_bytes": 1}
    ]
    with pytest.raises(ExecutionBlocked, match="占用"):
        execute_plan(
            ready_plan(),
            FakeTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: host,
        )


def test_allowed_action_cannot_smuggle_destructive_argv() -> None:
    plan = ready_plan()
    plan["steps"][0]["command"] = ["rm", "-rf", "/"]
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="与声明动作"):
        validate_executable_plan(plan)


def test_service_label_cannot_smuggle_docker_kill() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=["docker", "kill", "30011"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="精确 run 语法"):
        validate_executable_plan(plan)


def test_container_launch_binds_exact_gpu_ids() -> None:
    plan = ready_plan()
    image = "registry.example/h3@sha256:" + "a" * 64
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=[
            "docker", "run", "--name", "dep-1", "--gpus", "device=0",
            "--publish", "127.0.0.1:30011:30011", "--detach", image,
        ],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)
    plan["steps"][0]["command"][5] = "all"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="精确 run 语法"):
        validate_executable_plan(plan)


def test_native_service_label_requires_framework_launch_grammar() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=["sglang", "remove", "--port", "30011"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="已审核启动命令"):
        validate_executable_plan(plan)


def test_read_only_label_cannot_smuggle_python_or_docker() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="inspect",
        action_class="READ_ONLY",
        command=["python3", "-c", "import os; os.system('reboot')"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="与声明动作"):
        validate_executable_plan(plan)


def test_mutable_framework_low_tier_evidence_and_stage_skips_are_blocked() -> None:
    plan = ready_plan()
    plan["framework"]["version"] = "latest"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="不可变"):
        validate_executable_plan(plan)

    plan = ready_plan()
    plan["evidence"][0]["source"]["authority_tier"] = "D"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="S/A 级证据"):
        validate_executable_plan(plan)

    plan = ready_plan()
    plan["lifecycle"]["transitions"][2], plan["lifecycle"]["transitions"][3] = (
        plan["lifecycle"]["transitions"][3],
        plan["lifecycle"]["transitions"][2],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="生命周期前缀"):
        validate_executable_plan(plan)


def test_remote_writer_lock_blocks_second_controller(tmp_path):
    class ContendedTransport(FakeTransport):
        def run(self, argv, *, timeout=20, cwd=None, env=None):
            if tuple(argv) == ("mkdir", "/tmp/model-deployment-harness-writer-lock"):
                return CommandResult(tuple(argv), 1, "", "exists")
            return super().run(argv, timeout=timeout, cwd=cwd, env=env)

    with pytest.raises(ExecutionBlocked, match="远端写入锁已被持有"):
        execute_plan(
            ready_plan(),
            ContendedTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )


def test_lifecycle_envelope_requires_typed_hashed_stage_source() -> None:
    plan = ready_plan()
    plan["lifecycle"]["transitions"][0]["artifact"] = {
        "path": "README.md",
        "sha256": file_sha256(ROOT / "README.md"),
        "media_type": "text/markdown",
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="生命周期制品无效"):
        validate_executable_plan(plan)
