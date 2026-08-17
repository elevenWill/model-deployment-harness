from __future__ import annotations

import pytest
import yaml

from scripts._common import ROOT, HarnessError, canonical_plan_sha256, validate_instance
from scripts.preflight import host_preflight, requirement_gate
from scripts.registry import attach_observation, deployment_status_view
from scripts.remote_exec import (
    ExecutionBlocked,
    _host_matches_artifacts,
    authorize_execution,
    remote_writer_lock,
    validate_executable_plan,
)
from scripts.verify_service import evaluate_overall
from tests.test_remote_exec import deployment_request, observed_host, ready_plan


def _request() -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "eval-request",
        "requested_at": "2026-08-17T00:00:00Z",
        "requested_by": "eval-operator",
        "target": {
            "host": {"address": "10.0.0.25", "ssh_username": "deploy", "ssh_port": 22},
            "gpu_ids": [0],
            "install_root": "/srv/runtime",
            "model_root": "/data/models",
        },
        "model": {"id": "minimax-h3", "variant": "fl2va"},
        "framework_preference": "sglang",
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30010},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "internal evaluation",
        "deployment_region": "CN",
    }


def _host() -> dict:
    return {
        "host_id": "knode25",
        "probe": {"status": "COMPLETE"},
        "hardware": {"gpus": [{"index": 0, "uuid": "GPU-eval"}]},
        "software": {"nvidia": {"cuda_compatibility": "12.4"}},
        "network": {"listening_ports": [22]},
        "runtime": {"gpu_processes": []},
    }


def _levels() -> dict:
    return {
        level: {"status": "PASS", "checked_at": "2026-08-17T00:00:00Z", "detail": "eval"}
        for level in (
            "L1_environment",
            "L2_process",
            "L3_port",
            "L4_api",
            "L5_real_inference",
            "L6_output_validation",
        )
    }


def _deployment_record() -> dict:
    return {
        "schema_version": "1.0",
        "deployment_id": "dep-eval",
        "host_id": "knode25",
        "request_ref": "request.json",
        "plan_ref": "plan.json",
        "model": {"id": "minimax-h3", "variant": "fl2va", "path": "/data/models/H3"},
        "framework": {"name": "sglang", "version": "immutable-pin"},
        "target": {
            "gpu_ids": [0],
            "install_root": "/srv/runtime",
            "bind_host": "127.0.0.1",
            "port": 30010,
        },
        "deployment_status": "STARTED",
        "created_at": "2026-08-17T00:00:00Z",
        "updated_at": "2026-08-17T00:00:00Z",
        "known_state": {
            "recorded_at": "2026-08-17T00:00:00Z",
            "expected_service_state": "RUNNING",
            "expected_port": 30010,
        },
    }


def test_eval_1_missing_gpu_and_paths_stops_before_execution() -> None:
    result = requirement_gate({"target": {"host": {"address": "10.0.0.25"}}})
    assert result.status == "NEEDS_USER_INPUT"
    assert {"target.gpu_ids", "target.install_root", "target.model_root"} <= set(
        result.missing_fields
    )


def test_eval_2_complete_request_but_failed_ssh_is_blocked() -> None:
    host = _host()
    host["probe"]["status"] = "FAILED"
    assert host_preflight(
        _request(), host, environment_strategy="container", environment_isolated=True
    ).status == "BLOCKED"


def test_eval_3_occupied_gpu_is_blocked_without_kill() -> None:
    host = _host()
    host["runtime"]["gpu_processes"] = [
        {"gpu_id": "GPU-eval", "pid": 42, "process_name": "training"}
    ]
    result = host_preflight(
        _request(), host, environment_strategy="container", environment_isolated=True
    )
    assert result.status == "BLOCKED"
    assert "未停止任何进程" in " ".join(result.blockers)


def test_eval_4_cuda_mismatch_blocks_without_upgrade() -> None:
    result = host_preflight(
        _request(),
        _host(),
        required_cuda="12.6",
        environment_strategy="container",
        environment_isolated=True,
    )
    assert result.status == "BLOCKED"
    assert "请选择" in " ".join(result.recommendations)


def test_eval_5_official_source_beats_blog() -> None:
    policy = yaml.safe_load((ROOT / "config/source-policy.yaml").read_text(encoding="utf-8"))
    assert policy["rules"]["deployment_decisions_require_tiers"] == ["S", "A"]
    assert policy["tiers"]["D"]["decision_use"] == "prohibited_for_critical_decisions"
    assert policy["rules"]["conflicting_sources_prefer_higher_tier"] is True


def test_eval_6_registry_running_but_live_stopped_is_explicit_mismatch() -> None:
    observed = {
        "checked_at": "2026-08-17T01:00:00Z",
        "ssh": "PASS",
        "process": "FAIL",
        "port": "FAIL",
        "api": "NOT_CHECKED",
        "inference": "NOT_CHECKED",
    }
    view = deployment_status_view(attach_observation(_deployment_record(), observed))
    assert "已知状态 ≠ 观测状态" in view["live_summary"]


def test_eval_7_oom_inference_cannot_be_verified() -> None:
    levels = _levels()
    levels["L5_real_inference"]["status"] = "FAIL"
    assert evaluate_overall(levels, 12.0, True) == "FAILED"


def test_eval_8_untested_workaround_cannot_be_verified_lesson() -> None:
    lesson = {
        "schema_version": "1.0",
        "lesson_id": "lesson-eval",
        "created_at": "2026-08-17T00:00:00Z",
        "status": "VERIFIED",
        "statement": "untested workaround",
        "scope": {"model_ids": ["minimax-h3"], "frameworks": [], "hardware": []},
        "evidence_refs": [],
    }
    with pytest.raises(HarnessError):
        validate_instance(lesson, "lesson.schema.json")


def test_eval_9_license_region_risk_is_a_hard_gate() -> None:
    policy = yaml.safe_load((ROOT / "config/harness-policy.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load(
        (ROOT / "models/minimax-h3/manifest.yaml").read_text(encoding="utf-8")
    )
    assert "known_region_conflict" in policy["license_gate"]["block_on"]
    assert "US" in manifest["license"]["excluded_territories_without_separate_license"]
    assert manifest["license"]["gate_status_default"] == "NEEDS_REVIEW"


def test_eval_10_minimax_specific_logic_does_not_pollute_core() -> None:
    for path in (ROOT / "scripts").glob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert "minimax-h3" not in source, path


def test_eval_11_secrets_are_ignored_and_templates_are_empty() -> None:
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DEPLOY_SSH_PASSWORD=\n" in template
    assert "HF_TOKEN=\n" in template


def test_eval_12_forbidden_overarchitecture_is_absent() -> None:
    forbidden = {"langgraph", "fastapi", "redis", "psycopg", "kubernetes"}
    dependencies = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert all(name not in dependencies for name in forbidden)
    paths = {path.name.lower() for path in ROOT.iterdir()}
    assert not ({"web", "k8s", "rag"} & paths)


def test_eval_13_plan_action_label_cannot_bypass_command_policy() -> None:
    plan = ready_plan()
    plan["steps"][0]["command"] = ["reboot"]
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="与声明动作"):
        validate_executable_plan(plan)
    plan = ready_plan()
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=["docker", "kill", "30011"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="精确 run 语法"):
        validate_executable_plan(plan)


def test_eval_14_second_controller_cannot_acquire_remote_writer_lock() -> None:
    class LockedTransport:
        def run(self, argv, *, timeout=20, cwd=None, env=None):
            from scripts.probe_host import CommandResult

            return CommandResult(tuple(argv), 1, "", "exists")

    with (
        pytest.raises(ExecutionBlocked, match="远端写入锁已被持有"),
        remote_writer_lock(
            LockedTransport(),
            ("mkdir", "/tmp/model-deployment-harness-writer-lock"),
            ("rmdir", "/tmp/model-deployment-harness-writer-lock"),
        ),
    ):
        raise AssertionError("lock body must not run")


def test_eval_15_incident_is_structured_and_unverified_fix_cannot_resolve() -> None:
    incident = {
        "schema_version": "1.0",
        "incident_id": "incident-eval",
        "deployment_id": "dep-eval",
        "host_id": "knode25",
        "opened_at": "2026-08-17T00:00:00Z",
        "severity": "HIGH",
        "symptom": "inference OOM",
        "environment": "fixture",
        "status": "RESOLVED",
        "cause": {"status": "HYPOTHESIS", "description": "possibly insufficient HBM"},
        "fix": {"description": "enable offload", "verified": False},
        "timeline": [{"at": "2026-08-17T00:00:00Z", "event": "opened"}],
    }
    with pytest.raises(HarnessError):
        validate_instance(incident, "incident.schema.json")


def test_eval_16_same_harness_artifacts_port_to_a_second_host() -> None:
    plan = ready_plan()
    request = deployment_request()
    profile = observed_host()
    plan["target"]["host_id"] = "knode26"
    request["target"]["host"] = {
        "host_id": "knode26",
        "ssh_username": "deploy",
        "ssh_port": 22,
    }
    profile["host_id"] = "knode26"
    profile["identity"] = {"hostname": "knode26", "addresses": ["10.0.0.26"], "aliases": []}
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    assert authorize_execution(plan, request, profile).status == "PASS"


def test_eval_17_not_checked_live_state_is_never_ok() -> None:
    observed = {
        "checked_at": "2026-08-17T01:00:00Z",
        "ssh": "PASS",
        "process": "NOT_CHECKED",
        "port": "NOT_CHECKED",
        "api": "NOT_CHECKED",
        "inference": "NOT_CHECKED",
    }
    view = deployment_status_view(attach_observation(_deployment_record(), observed))
    assert view["live_summary"].startswith("INCOMPLETE/NOT_CHECKED")


def test_eval_18_request_region_and_host_selector_are_bound() -> None:
    plan = ready_plan()
    request = deployment_request()
    request["deployment_region"] = "US"
    with pytest.raises(ExecutionBlocked, match="许可门禁区域"):
        authorize_execution(plan, request, observed_host())
    address_request = deployment_request()
    address_request["target"]["host"] = {
        "address": "10.0.0.25",
        "ssh_username": "deploy",
        "ssh_port": 22,
    }
    assert _host_matches_artifacts("10.0.0.26", address_request, observed_host()) is False
