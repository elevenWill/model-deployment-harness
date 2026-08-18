from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts._common import (
    ROOT,
    HarnessError,
    canonical_plan_sha256,
    file_sha256,
    load_document,
    validate_instance,
)
from scripts.deployment_archive import DeploymentArchive
from scripts.probe_host import CommandResult
from scripts.remote_exec import (
    ExecutionBlocked,
    _validate_adaptation_binding,
    _validate_capacity_trial_actions,
    _validate_catalog_limits,
    _validate_catalog_request_limits,
    archive_reviewed_lifecycle,
    authorize_execution,
    execute_plan,
    validate_executable_plan,
)
from scripts.run_inference import resolve_output_binding


def _write_json_artifact(root: Path, name: str, document: dict) -> dict[str, str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return {"path": name, "sha256": file_sha256(path)}


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
        "purpose": "STANDARD",
        "deployment_id": "dep-1",
        "request_id": "req-1",
        "created_at": "2026-08-17T00:00:00Z",
        "host_profile_observed_at": "2026-08-17T00:01:00Z",
        "target": {
            "host_id": "knode25",
            "gpu_ids": [0, 1, 2, 3],
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
        "compatibility": {
            "basis": "CATALOG_PROFILE",
            "profile_id": "sglang-4xh200-resident",
            "required_cuda": "12.6",
            "catalog_limits": {
                "max_concurrency": 1,
                "max_short_edge": 768,
                "max_duration_seconds": 15,
                "selected_concurrency": 1,
                "selected_short_edge": 768,
                "selected_duration_seconds": 5,
                "variant": "fl2va",
                "input_kind": "none",
            },
        },
        "environment": {"strategy": "container", "isolated": True, "rationale": "preserve host"},
        "service": {
            "mode": "container",
            "bind_host": "127.0.0.1",
            "port": 30011,
            "max_concurrency": 1,
        },
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
            "gpu_ids": [0, 1, 2, 3],
            "install_root": "/opt/models",
            "model_root": "/models",
        },
        "model": {"id": "minimax-h3", "variant": "fl2va"},
        "framework_preference": "sglang",
        "service": {
            "mode": "container",
            "bind_host": "127.0.0.1",
            "port": 30011,
            "max_concurrency": 1,
        },
        "inference": {"concurrency": 1, "short_edge": 768, "duration_seconds": 5},
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
            "memory": {"total_bytes": 512 * 1024**3, "available_bytes": 400 * 1024**3},
            "gpus": [
                {
                    "index": index,
                    "uuid": f"GPU-h200-{index}",
                    "model": "NVIDIA H200",
                    "memory_total_bytes": 141 * 1024**3,
                    "memory_free_bytes": 140 * 1024**3,
                }
                for index in range(4)
            ],
            "gpu_topology": [
                {"gpu_a": f"GPU-h200-{a}", "gpu_b": f"GPU-h200-{b}", "link": "NV4"}
                for a in range(4)
                for b in range(a + 1, 4)
            ],
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


def comfy_host():
    host = observed_host()
    host["hardware"]["gpus"] = [
        {
            "index": 0,
            "uuid": "GPU-rtx3090-0",
            "model": "NVIDIA GeForce RTX 3090",
            "memory_total_bytes": 24 * 1024**3,
            "memory_free_bytes": 23 * 1024**3,
        }
    ]
    host["hardware"]["gpu_topology"] = []
    return host


def rtx5090_catalog_case(available_gib: int) -> tuple[dict, dict, dict]:
    plan = ready_plan()
    plan["target"]["gpu_ids"] = [0, 1]
    plan["compatibility"]["profile_id"] = "sglang-2xrtx5090-offload"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    request = deployment_request()
    request["target"]["gpu_ids"] = [0, 1]
    host = observed_host()
    host["hardware"]["gpus"] = [
        {
            "index": index,
            "uuid": f"GPU-rtx5090-{index}",
            "model": "NVIDIA GeForce RTX 5090",
            "memory_total_bytes": 32 * 1024**3,
            "memory_free_bytes": 31 * 1024**3,
        }
        for index in range(2)
    ]
    host["hardware"]["gpu_topology"] = [
        {"gpu_a": "GPU-rtx5090-0", "gpu_b": "GPU-rtx5090-1", "link": "PXB"}
    ]
    host["hardware"]["memory"] = {
        "total_bytes": 512 * 1024**3,
        "available_bytes": available_gib * 1024**3,
    }
    return plan, request, host


def adaptive_ready_plan(tmp_path: Path, monkeypatch) -> dict:
    """Build a fully hashed local trial chain without touching a real host."""
    import scripts.remote_exec as remote_exec

    monkeypatch.setattr(remote_exec, "ROOT", tmp_path)
    monkeypatch.setattr("scripts.preflight.validate_media", lambda *_: (True, "fixture"))
    request_ref = _write_json_artifact(tmp_path, "request.json", deployment_request())
    host = observed_host()
    host_ref = _write_json_artifact(tmp_path, "host.json", host)
    output_ref = _write_json_artifact(tmp_path, "trial-output.json", {"output": "decodable"})
    trial_plan = ready_plan()
    trial_plan["purpose"] = "CAPACITY_TRIAL"
    trial_plan["compatibility"]["basis"] = "CAPACITY_TRIAL"
    trial_plan["deployment_id"] = "trial-dep-1"
    evidence = deepcopy(trial_plan["evidence"][0])
    evidence["supports_gap_ids"] = ["gpu-profile"]
    evidence["supports_mechanism_ids"] = ["low-memory-mode"]
    trial_plan["evidence"] = [evidence]
    pretrial_candidate = {
        "candidate_id": "low-vram",
        "description": "validated low-memory mode",
        "applies_to_gap_ids": ["gpu-profile"],
        "evidence_ids": ["ev-1"],
        "mitigation_mechanisms": [
            {
                "mechanism_id": "low-memory-mode",
                "description": "reduce memory pressure",
                "addresses_gap_ids": ["gpu-profile"],
                "evidence_ids": ["ev-1"],
            }
        ],
        "applicability_checks": [{"name": "fixture host", "status": "PASS"}],
        "local_reproduction": {"status": "NOT_RUN"},
        "plan_conditions": ["target root remains available"],
    }
    pretrial_assessment = {
        "schema_version": "1.0",
        "assessment_id": "adapt-1",
        "request_id": "req-1",
        "request_artifact": request_ref,
        "host_id": "knode25",
        "host_profile_observed_at": "2026-08-17T00:01:00Z",
        "host_profile_artifact": host_ref,
        "adaptation_status": "READY_FOR_TRIAL",
        "next_stage": "PLAN",
        "gaps": [
            {
                "gap_id": "gpu-profile",
                "category": "recommended_profile_mismatch",
                "description": "fixture GPU is not a recommended profile",
            }
        ],
        "research": {
            "status": "COMPLETED",
            "selected_candidate_id": "low-vram",
            "candidates": [pretrial_candidate],
        },
        "evidence": [evidence],
    }
    pretrial_ref = _write_json_artifact(tmp_path, "pretrial-assessment.json", pretrial_assessment)
    trial_plan["compatibility"]["adaptation"] = {
        "assessment_ref": pretrial_ref,
        "assessment_id": "adapt-1",
        "candidate_id": "low-vram",
        "plan_conditions": [
            {
                "condition": "target root remains available",
                "preflight_step_id": "trial-condition",
            }
        ],
    }
    trial_plan["steps"][0]["sequence"] = 2
    trial_plan["steps"][0]["depends_on"] = ["trial-condition"]
    trial_plan["steps"].insert(
        0,
        {
            "step_id": "trial-condition",
            "sequence": 1,
            "name": "verify trial condition",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["test", "-d", "/opt/models"],
            "depends_on": [],
            "success_criteria": ["target root remains available"],
            "rollback_step_ids": [],
        },
    )
    trial_plan["review"]["plan_sha256"] = canonical_plan_sha256(trial_plan)
    trial_plan_ref = _write_json_artifact(tmp_path, "trial-plan.json", trial_plan)
    trial_hash = canonical_plan_sha256(trial_plan)
    execution_ref = _write_json_artifact(
        tmp_path,
        "trial-execution.json",
        {
            "schema_version": "1.0",
            "execution_id": "trial-exec-1",
            "producer": "HARNESS_PLAN_EXECUTOR",
            "deployment_id": "trial-dep-1",
            "host_id": "knode25",
            "plan_sha256": trial_hash,
            "started_at": "2026-08-17T00:02:00Z",
            "completed_at": "2026-08-17T00:03:00Z",
            "status": "EXECUTED",
            "steps": [
                {
                    "step_id": step["step_id"],
                    "started_at": "2026-08-17T00:02:00Z",
                    "completed_at": "2026-08-17T00:03:00Z",
                    "returncode": 0,
                    "stdout_redacted": "ok",
                    "stderr_redacted": "",
                }
                for step in trial_plan["steps"]
            ],
        },
    )
    payload_ref = _write_json_artifact(tmp_path, "trial-payload.json", {"prompt": "fixture"})
    payload_ref["path"] = str(tmp_path / "trial-payload.json")
    payload_ref["media_type"] = "application/json"
    output_ref["path"] = str(tmp_path / "trial-output.json")
    output_ref["media_type"] = "video/mp4"
    response_document = {
        "id": "job-1",
        "status": "COMPLETED",
        "output": {
            "id": "artifact-1",
            "url": "/outputs/artifact-1",
            "sha256": output_ref["sha256"],
        },
    }
    response_ref = _write_json_artifact(tmp_path, "trial-response.json", response_document)
    response_ref["path"] = str(tmp_path / "trial-response.json")
    response_ref["media_type"] = "application/json"
    inference_api = load_document(ROOT / "models/minimax-h3/verify.yaml")["inference_api"]
    proof_ref = _write_json_artifact(
        tmp_path,
        "trial-proof.json",
        {
            "schema_version": "1.0",
            "producer": "HARNESS_HTTP_RUNNER",
            "deployment_id": "trial-dep-1",
            "plan_sha256": trial_hash,
            "endpoint": "http://127.0.0.1:30011",
            "request": {
                "method": "POST",
                "path": "/v1/videos",
                "payload": payload_ref,
                "submitted_at": "2026-08-17T00:03:59Z",
            },
            "job": {
                "job_id": "job-1",
                "status": "COMPLETED",
                "completed_at": "2026-08-17T00:04:00Z",
                "response": response_ref,
                "runtime_error": None,
            },
            "output_binding": resolve_output_binding(
                response_document,
                "job-1",
                inference_api,
                "http://127.0.0.1:30011/",
            )
            | {
                "downloaded_at": "2026-08-17T00:04:01Z",
                "download_sha256": output_ref["sha256"],
                "download_content_length": str((tmp_path / "trial-output.json").stat().st_size),
                "response_headers": {
                    "content_length": str((tmp_path / "trial-output.json").stat().st_size)
                },
            },
            "output": output_ref,
        },
    )
    semantic_ref = _write_json_artifact(
        tmp_path,
        "trial-semantic.json",
        {
            "schema_version": "1.0",
            "review_id": "semantic-1",
            "deployment_id": "trial-dep-1",
            "plan_sha256": trial_hash,
            "output_sha256": output_ref["sha256"],
            "reviewed_by": "fixture-reviewer",
            "reviewed_at": "2026-08-17T00:04:00Z",
            "checks": {
                "not_blank_or_frozen": "PASS",
                "audio_present_not_silent": "PASS",
                "task_alignment": "PASS",
            },
        },
    )
    check = {
        "status": "PASS",
        "checked_at": "2026-08-17T00:04:00Z",
        "detail": "fixture passed",
    }
    verification_ref = _write_json_artifact(
        tmp_path,
        "trial-verification.json",
        {
            "schema_version": "1.0",
            "verification_id": "trial-verify-1",
            "deployment_id": "trial-dep-1",
            "host_id": "knode25",
            "plan_sha256": trial_hash,
            "recipe_ref": "models/minimax-h3/verify.yaml",
            "started_at": "2026-08-17T00:03:00Z",
            "completed_at": "2026-08-17T00:04:00Z",
            "levels": {
                "L1_environment": check,
                "L2_process": check,
                "L3_port": check,
                "L4_api": check,
                "L5_real_inference": check | {"evidence": [output_ref]},
                "L6_output_validation": check | {"evidence": [output_ref]},
            },
            "overall_status": "VERIFIED",
            "metrics": {"generation_duration_seconds": 1.0},
            "framework": {
                "name": trial_plan["framework"]["name"],
                "version": trial_plan["framework"]["version"],
            },
            "gpu_topology_summary": "fixture",
            "command_redacted": ["fixture"],
            "artifacts": [proof_ref, semantic_ref, output_ref],
        },
    )
    assessment = {
        "schema_version": "1.0",
        "assessment_id": "adapt-1",
        "request_id": "req-1",
        "request_artifact": request_ref,
        "host_id": "knode25",
        "host_profile_observed_at": "2026-08-17T00:01:00Z",
        "host_profile_artifact": host_ref,
        "adaptation_status": "VALIDATED",
        "next_stage": "PLAN",
        "gaps": [
            {
                "gap_id": "gpu-profile",
                "category": "recommended_profile_mismatch",
                "description": "fixture GPU is not a recommended profile",
            }
        ],
        "research": {
            "status": "COMPLETED",
            "selected_candidate_id": "low-vram",
            "candidates": [
                {
                    "candidate_id": "low-vram",
                    "description": "validated low-memory mode",
                    "applies_to_gap_ids": ["gpu-profile"],
                    "evidence_ids": ["ev-1"],
                    "mitigation_mechanisms": [
                        {
                            "mechanism_id": "low-memory-mode",
                            "description": "reduce memory pressure",
                            "addresses_gap_ids": ["gpu-profile"],
                            "evidence_ids": ["ev-1"],
                        }
                    ],
                    "applicability_checks": [{"name": "fixture host", "status": "PASS"}],
                    "local_reproduction": {
                        "status": "PASS",
                        "trial_evidence": {
                            "checked_at": "2026-08-17T00:04:00Z",
                            "trial_deployment_id": "trial-dep-1",
                            "trial_plan": trial_plan_ref,
                            "execution_record": execution_ref,
                            "inference_proof": proof_ref,
                            "semantic_review": semantic_ref,
                            "verification_result": verification_ref,
                        },
                    },
                    "plan_conditions": ["target root remains available"],
                }
            ],
        },
        "evidence": [evidence],
    }
    assessment_ref = _write_json_artifact(tmp_path, "assessment.json", assessment)
    plan = ready_plan()
    plan["evidence"] = [evidence]
    condition_step = {
        "step_id": "adaptation-preflight",
        "sequence": 1,
        "name": "verify adaptation condition",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["test", "-d", "/opt/models"],
        "success_criteria": ["target root remains available"],
        "rollback_step_ids": [],
    }
    plan["steps"][0]["sequence"] = 2
    plan["steps"][0]["depends_on"] = ["adaptation-preflight"]
    plan["steps"].insert(0, condition_step)
    plan["compatibility"]["adaptation"] = {
        "assessment_ref": assessment_ref,
        "assessment_id": "adapt-1",
        "candidate_id": "low-vram",
        "plan_conditions": [
            {
                "condition": "target root remains available",
                "preflight_step_id": "adaptation-preflight",
            }
        ],
    }
    plan["compatibility"]["basis"] = "VALIDATED_ADAPTATION"
    return plan


def test_adaptive_plan_binds_validated_trial_chain_and_prewrite_check(
    tmp_path, monkeypatch
) -> None:
    plan = adaptive_ready_plan(tmp_path, monkeypatch)
    validate_instance(plan, "deployment-plan.schema.json")
    _validate_adaptation_binding(plan)


def test_adaptive_plan_rejects_trial_runtime_or_condition_dependency_drift(
    tmp_path, monkeypatch
) -> None:
    plan = adaptive_ready_plan(tmp_path, monkeypatch)
    plan["framework"]["version"] = "b" * 40
    with pytest.raises(ExecutionBlocked, match="不可变运行时"):
        _validate_adaptation_binding(plan)

    plan = adaptive_ready_plan(tmp_path, monkeypatch)
    plan["steps"][-1]["depends_on"] = []
    with pytest.raises(ExecutionBlocked, match="每个远程写步骤"):
        _validate_adaptation_binding(plan)


def test_adaptive_plan_rejects_assessment_hash_drift(tmp_path, monkeypatch) -> None:
    plan = adaptive_ready_plan(tmp_path, monkeypatch)
    plan["compatibility"]["adaptation"]["assessment_ref"]["sha256"] = "0" * 64
    with pytest.raises(ExecutionBlocked, match="SHA-256"):
        _validate_adaptation_binding(plan)


def test_adaptive_plan_revalidates_trial_execution_and_verification_artifacts(
    tmp_path, monkeypatch
) -> None:
    plan = adaptive_ready_plan(tmp_path, monkeypatch)
    assessment_path = tmp_path / plan["compatibility"]["adaptation"]["assessment_ref"]["path"]
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    trial = assessment["research"]["candidates"][0]["local_reproduction"]["trial_evidence"]
    trial["execution_record"]["sha256"] = "0" * 64
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    plan["compatibility"]["adaptation"]["assessment_ref"]["sha256"] = file_sha256(assessment_path)

    with pytest.raises(ExecutionBlocked, match="适配评估"):
        _validate_adaptation_binding(plan)


def test_capacity_trial_requires_ready_assessment_and_exact_candidate_binding(
    tmp_path, monkeypatch
) -> None:
    adaptive_ready_plan(tmp_path, monkeypatch)
    trial_plan = json.loads((tmp_path / "trial-plan.json").read_text(encoding="utf-8"))
    validate_instance(trial_plan, "deployment-plan.schema.json")
    _validate_adaptation_binding(trial_plan)

    without_assessment = deepcopy(trial_plan)
    del without_assessment["compatibility"]["adaptation"]
    with pytest.raises(HarnessError):
        validate_instance(without_assessment, "deployment-plan.schema.json")

    assessment_path = tmp_path / "pretrial-assessment.json"
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["research"]["selected_candidate_id"] = "different-candidate"
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    trial_plan["compatibility"]["adaptation"]["assessment_ref"]["sha256"] = file_sha256(
        assessment_path
    )
    with pytest.raises(ExecutionBlocked, match="评估|候选"):
        _validate_adaptation_binding(trial_plan)


def test_capacity_trial_rejects_actions_outside_isolated_trial_scope(tmp_path, monkeypatch) -> None:
    adaptive_ready_plan(tmp_path, monkeypatch)
    trial_plan = json.loads((tmp_path / "trial-plan.json").read_text(encoding="utf-8"))
    trial_plan["steps"][-1]["action"] = "other"
    trial_plan["steps"][-1]["action_class"] = "PLAN_ALLOWED_WRITE"
    with pytest.raises(ExecutionBlocked, match="容量试跑包含超出"):
        _validate_capacity_trial_actions(trial_plan)


def test_catalog_profile_rejects_gpu_count_model_vram_and_topology_bypasses() -> None:
    plan = ready_plan()
    request = deployment_request()
    one_gpu = observed_host()
    one_gpu["hardware"]["gpus"] = one_gpu["hardware"]["gpus"][:1]
    one_gpu["hardware"]["gpu_topology"] = []
    plan["target"]["gpu_ids"] = [0]
    request["target"]["gpu_ids"] = [0]
    with pytest.raises(ExecutionBlocked, match="GPU 数量"):
        authorize_execution(plan, request, one_gpu)

    wrong_model = observed_host()
    for gpu in wrong_model["hardware"]["gpus"]:
        gpu["model"] = "fixture GPU"
    with pytest.raises(ExecutionBlocked, match="GPU 型号"):
        authorize_execution(ready_plan(), deployment_request(), wrong_model)

    low_vram = observed_host()
    low_vram["hardware"]["gpus"][0]["memory_total_bytes"] = 80 * 1024**3
    with pytest.raises(ExecutionBlocked, match="显存"):
        authorize_execution(ready_plan(), deployment_request(), low_vram)

    disconnected = observed_host()
    disconnected["hardware"]["gpu_topology"] = []
    with pytest.raises(ExecutionBlocked, match="拓扑"):
        authorize_execution(ready_plan(), deployment_request(), disconnected)


def test_catalog_profile_host_ram_rejects_missing_and_below_minimum_but_accepts_boundary() -> None:
    plan, request, boundary = rtx5090_catalog_case(200)
    assert authorize_execution(plan, request, boundary).status == "PASS"

    missing = deepcopy(boundary)
    del missing["hardware"]["memory"]
    with pytest.raises(ExecutionBlocked, match="缺少.*可用内存"):
        authorize_execution(plan, request, missing)

    _plan, _request, below = rtx5090_catalog_case(199)
    with pytest.raises(ExecutionBlocked, match="可用内存低于"):
        authorize_execution(plan, request, below)


def test_catalog_profile_rechecks_available_ram_from_live_host_before_writes(tmp_path) -> None:
    plan, request, reviewed = rtx5090_catalog_case(200)
    _plan, _request, live = rtx5090_catalog_case(199)
    with pytest.raises(ExecutionBlocked, match="可用内存低于"):
        execute_plan(
            plan,
            FakeTransport(),
            request=request,
            host_profile=reviewed,
            lock_directory=tmp_path,
            _probe_collector=lambda _: live,
        )


def test_catalog_inference_limits_reject_missing_unknown_and_over_limit_at_boundary(
    monkeypatch,
) -> None:
    plan = ready_plan()
    request = deployment_request()
    plan["compatibility"]["catalog_limits"]["selected_duration_seconds"] = 15
    request["inference"]["duration_seconds"] = 15
    _validate_catalog_request_limits(plan, request)

    plan["compatibility"]["catalog_limits"]["selected_duration_seconds"] = 16
    request["inference"]["duration_seconds"] = 16
    with pytest.raises(ExecutionBlocked, match="超过目录限制"):
        _validate_catalog_request_limits(plan, request)

    missing = ready_plan()
    del missing["compatibility"]["catalog_limits"]
    with pytest.raises(HarnessError):
        validate_instance(missing, "deployment-plan.schema.json")

    missing_service_limit = ready_plan()
    del missing_service_limit["service"]["max_concurrency"]
    with pytest.raises(ExecutionBlocked, match="服务并发配置"):
        _validate_catalog_limits(missing_service_limit)

    original_profile = {
        "limits": {
            "max_concurrency": 1,
            "max_short_edge": 768,
            "max_duration_seconds": 15,
            "allowed_variants": ["fl2va"],
            "input_authorization": {
                "required_variants": [],
                "input_kind": "local_reference",
            },
            "unknown_physical_limit": 1,
        }
    }
    monkeypatch.setattr("scripts.remote_exec._catalog_profile", lambda _plan: original_profile)
    with pytest.raises(ExecutionBlocked, match="未知字段"):
        _validate_catalog_limits(ready_plan())


def test_ref2va_input_authorization_binds_request_license_and_plan() -> None:
    plan = ready_comfyui_plan()
    request = deployment_request()
    request["inference"] = {
        "concurrency": 1,
        "short_edge": 768,
        "duration_seconds": 4,
        "input_authorization_reference": "fixture-reference-input-auth",
    }
    _validate_catalog_request_limits(plan, request)

    del request["inference"]["input_authorization_reference"]
    with pytest.raises(ExecutionBlocked, match="精确推理范围"):
        _validate_catalog_request_limits(plan, request)

    request["inference"]["input_authorization_reference"] = "fixture-reference-input-auth"
    plan["license_gate"]["authorization_reference"] = "different-authorization"
    with pytest.raises(ExecutionBlocked, match="请求、许可门禁和计划"):
        _validate_catalog_request_limits(plan, request)


def ready_comfyui_plan():
    plan = ready_plan()
    install_root = "/opt/h3"
    runtime_root = f"{install_root}/ComfyUI"
    model_root = f"{install_root}/model"
    venv_python = f"{install_root}/.venv/bin/python"
    bootstrap_uv = "/opt/toolchain/bin/uv"
    bootstrap_python = "/opt/toolchain/python/bin/python3.11"
    revision = "0d80858061b511bd38c8cef4c235ef8e01040822"
    plan["target"] = {
        "host_id": "knode25",
        "gpu_ids": [0],
        "install_root": install_root,
        "model_root": model_root,
    }
    plan["model"]["variant"] = "both"
    plan["framework"] = {
        "name": "comfyui",
        "version": revision,
        "recipe_ref": "models/minimax-h3/recipes/comfyui.yaml",
        "runtime_artifact": {
            "kind": "source_checkout",
            "location": runtime_root,
            "revision": revision,
            "probe_command": [
                "git",
                "-C",
                runtime_root,
                "rev-parse",
                "HEAD",
            ],
            "executable": venv_python,
        },
        "rationale": "isolated experimental ComfyUI service",
        "evidence_ids": ["ev-1"],
    }
    plan["compatibility"] = {
        "basis": "CATALOG_PROFILE",
        "profile_id": "comfyui-1xrtx3090-int8-convrot-experimental",
        "required_cuda": "12.6",
        "catalog_limits": {
            "max_concurrency": 1,
            "max_short_edge": 768,
            "max_duration_seconds": 4,
            "selected_concurrency": 1,
            "selected_short_edge": 768,
            "selected_duration_seconds": 4,
            "variant": "both",
            "input_kind": "local_reference",
            "input_authorization_reference": "fixture-reference-input-auth",
        },
    }
    plan["license_gate"]["authorization_reference"] = "fixture-reference-input-auth"
    plan["environment"] = {
        "strategy": "venv",
        "isolated": True,
        "rationale": "preserve host",
        "bootstrap_uv": bootstrap_uv,
        "bootstrap_python": bootstrap_python,
    }
    plan["service"] = {
        "mode": "managed_service",
        "bind_host": "0.0.0.0",
        "port": 8188,
        "max_concurrency": 1,
    }
    model_files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    dependency_argv = [
        bootstrap_uv,
        "pip",
        "install",
        "--python",
        venv_python,
        "--index-url",
        "https://download.pytorch.org/whl/cu121",
        "--extra-index-url",
        "https://pypi.org/simple",
        "torch==2.5.1",
        "torchvision==0.20.1",
        "torchaudio==2.5.1",
        "https://files.pythonhosted.org/packages/27/d1/e53410260b81610233cb56c2fac1a9f3d39887be3cbb983cd8baa6a07528/comfy_kitchen-0.2.31-py3-none-any.whl#sha256=5117946c30f308cfc73b9c26f723ae3918308bd090e57a8eae298406934aabd6",
        "-r",
        f"{runtime_root}/requirements.txt",
        "setuptools==80.9.0",
        "modelscope==1.31.0",
    ]
    commands = [
        ("bootstrap-uv", "inspect", ["test", "-x", bootstrap_uv]),
        ("bootstrap-python", "inspect", ["test", "-x", bootstrap_python]),
        ("systemd-ready", "inspect", ["systemctl", "--user", "is-system-running"]),
        (
            "clone",
            "clone_source_checkout",
            [
                "git",
                "clone",
                "--no-checkout",
                "https://github.com/Comfy-Org/ComfyUI.git",
                runtime_root,
            ],
        ),
        (
            "checkout",
            "checkout_source_revision",
            ["git", "-C", runtime_root, "checkout", "--detach", revision],
        ),
        (
            "venv",
            "create_isolated_venv",
            [bootstrap_uv, "venv", "--python", bootstrap_python, f"{install_root}/.venv"],
        ),
        (
            "deps",
            "install_isolated_dependencies",
            dependency_argv,
        ),
        (
            "download",
            "download_model",
            [
                f"{install_root}/.venv/bin/modelscope",
                "download",
                "--model",
                "Comfy-Org/MiniMax-H3",
                "--revision",
                "a" * 40,
                "--local_dir",
                model_root,
                *model_files,
            ],
        ),
        (
            "models-link",
            "create_service_config",
            ["ln", "-s", model_root, f"{install_root}/models"],
        ),
        (
            "start",
            "start_own_service",
            [
                "systemd-run",
                "--user",
                "--unit=dep-1",
                "--collect",
                f"--property=WorkingDirectory={runtime_root}",
                "--setenv=CUDA_VISIBLE_DEVICES=0",
                venv_python,
                f"{runtime_root}/main.py",
                "--listen",
                "0.0.0.0",
                "--port",
                "8188",
                "--base-directory",
                install_root,
                "--lowvram",
                "--disable-auto-launch",
            ],
        ),
    ]
    plan["steps"] = [
        {
            "step_id": step_id,
            "sequence": index,
            "name": step_id,
            "action": action,
            "action_class": "READ_ONLY" if action == "inspect" else "PLAN_ALLOWED_WRITE",
            "command": command,
            "depends_on": [] if index == 1 else [commands[index - 2][0]],
            "success_criteria": ["fixture"],
            "rollback_step_ids": [],
        }
        for index, (step_id, action, command) in enumerate(commands, start=1)
    ]
    plan["steps"][-1]["depends_on"].append("systemd-ready")
    venv_step = next(step for step in plan["steps"] if step["step_id"] == "venv")
    venv_step["depends_on"] = ["checkout", "bootstrap-uv", "bootstrap-python"]
    plan["required_changes"] = [
        {
            "description": step["name"],
            "action_class": "PLAN_ALLOWED_WRITE",
            "step_ids": [step["step_id"]],
        }
        for step in plan["steps"]
    ]
    plan["verification"]["recipe_ref"] = "models/minimax-h3/verify-comfyui.yaml"
    plan["rollback"] = {
        "trigger_conditions": ["start or verification failure"],
        "steps": [
            {
                "step_id": "stop-comfyui",
                "sequence": 1,
                "name": "stop owned unit",
                "action": "stop_own_service",
                "action_class": "PLAN_ALLOWED_WRITE",
                "command": ["systemctl", "--user", "stop", "dep-1.service"],
                "success_criteria": ["owned unit stopped"],
                "rollback_step_ids": [],
            }
        ],
        "preserve_evidence": True,
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    return plan


class FakeTransport:
    def __init__(self, returncode=0, raised=None):
        self.calls = []
        self.uploads = []
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

    def upload_new(self, source, destination, *, timeout=300):
        self.uploads.append((source, destination, timeout))

    def close(self):
        pass


def test_staged_source_bundle_requires_exclusive_transfer_and_hashes(tmp_path):
    plan = ready_comfyui_plan()
    bundle = tmp_path / "comfyui.bundle"
    bundle.write_bytes(b"verified source bundle")
    bundle_sha = file_sha256(bundle)
    tree = "e2791b95dc97f50ef97a22499131160b605edd47"
    runtime = plan["framework"]["runtime_artifact"]
    runtime["source_bundle"] = {
        "local_path": str(bundle),
        "remote_path": "/opt/h3/.comfyui.bundle",
        "sha256": bundle_sha,
        "tree": tree,
    }
    replacements = {
        "clone": (
            "clone-source",
            "clone_source_checkout",
            ["git", "clone", "--no-checkout", "/opt/h3/.comfyui.bundle", "/opt/h3/ComfyUI"],
        ),
        "checkout": (
            "checkout-source",
            "checkout_source_revision",
            ["git", "-C", "/opt/h3/ComfyUI", "checkout", "--detach", runtime["revision"]],
        ),
    }
    rebuilt = []
    for step in plan["steps"]:
        replacement = replacements.get(step["step_id"])
        if replacement is None:
            rebuilt.append(step)
            continue
        step_id, action, command = replacement
        step["step_id"], step["action"], step["command"] = step_id, action, command
        rebuilt.append(step)
    rebuilt[3:3] = [
        {
            "step_id": "source-bundle-absent",
            "sequence": 0,
            "name": "source bundle absent",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["test", "!", "-e", "/opt/h3/.comfyui.bundle"],
            "depends_on": ["systemd-ready"],
            "success_criteria": ["absent"],
            "rollback_step_ids": [],
        },
        {
            "step_id": "stage-source-bundle",
            "sequence": 0,
            "name": "stage source bundle",
            "action": "stage_source_bundle",
            "action_class": "PLAN_ALLOWED_WRITE",
            "command": ["sftp-upload", str(bundle), "/opt/h3/.comfyui.bundle"],
            "depends_on": ["source-bundle-absent"],
            "success_criteria": ["exclusive upload"],
            "rollback_step_ids": [],
        },
        {
            "step_id": "verify-source-bundle",
            "sequence": 0,
            "name": "verify source bundle",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["sha256sum", "/opt/h3/.comfyui.bundle"],
            "depends_on": ["stage-source-bundle"],
            "success_criteria": ["sha matches"],
            "rollback_step_ids": [],
        },
    ]
    checkout_index = next(
        index for index, step in enumerate(rebuilt) if step["step_id"] == "checkout-source"
    )
    rebuilt.insert(
        checkout_index + 1,
        {
            "step_id": "verify-source-tree",
            "sequence": 0,
            "name": "verify source tree",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["git", "-C", "/opt/h3/ComfyUI", "rev-parse", "HEAD^{tree}"],
            "depends_on": ["checkout-source"],
            "success_criteria": ["tree matches"],
            "rollback_step_ids": [],
        },
    )
    for index, step in enumerate(rebuilt, start=1):
        step["sequence"] = index
    clone = next(step for step in rebuilt if step["step_id"] == "clone-source")
    clone["depends_on"] = ["verify-source-bundle"]
    for step in rebuilt:
        step["depends_on"] = [
            {"clone": "clone-source", "checkout": "checkout-source"}.get(item, item)
            for item in step.get("depends_on", [])
        ]
    plan["steps"] = rebuilt
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)

    class BundleTransport(FakeTransport):
        def run(self, argv, **kwargs):
            if tuple(argv) == ("sha256sum", "/opt/h3/.comfyui.bundle"):
                return CommandResult(tuple(argv), 0, f"{bundle_sha}  .comfyui.bundle\n", "")
            if tuple(argv) == ("git", "-C", "/opt/h3/ComfyUI", "rev-parse", "HEAD^{tree}"):
                return CommandResult(tuple(argv), 0, f"{tree}\n", "")
            if tuple(argv)[-2:] == ("rev-parse", "HEAD"):
                return CommandResult(tuple(argv), 0, f"{runtime['revision']}\n", "")
            return super().run(argv, **kwargs)

    transport = BundleTransport()
    result = execute_plan(
        plan,
        transport,
        request=deployment_request()
        | {
            "framework_preference": "comfyui",
            "target": deployment_request()["target"]
            | {"gpu_ids": [0], "install_root": "/opt/h3", "model_root": "/opt/h3/model"},
            "model": {"id": "minimax-h3", "variant": "both"},
            "service": plan["service"],
            "inference": {
                "concurrency": 1,
                "short_edge": 768,
                "duration_seconds": 4,
                "input_authorization_reference": "fixture-reference-input-auth",
            },
        },
        host_profile=comfy_host(),
        _probe_collector=lambda _transport: comfy_host(),
    )
    assert result.status == "EXECUTED"
    assert transport.uploads == [(bundle, "/opt/h3/.comfyui.bundle", 300)]


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


def test_comfyui_plan_only_allows_exact_modelscope_assets_and_isolated_service() -> None:
    plan = ready_comfyui_plan()
    validate_executable_plan(plan)
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    download["command"][-1] = "diffusion_models/minimax_h3_fl2va_bf16.safetensors"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="ModelScope"):
        validate_executable_plan(plan)


def test_comfyui_master_download_requires_postdownload_integrity_check() -> None:
    plan = ready_comfyui_plan()
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    revision = download["command"].index("--revision")
    download["command"][revision + 1] = "master"
    files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 90,
        "name": "verify ModelScope asset integrity",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": ["download"],
        "success_criteria": ["all SHA-256 values match the observed master manifest"],
        "rollback_step_ids": [],
    }
    link = next(step for step in plan["steps"] if step["action"] == "create_service_config")
    link["depends_on"].append("verify-model-assets")
    plan["steps"].append(checksum)
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)

    checksum["depends_on"] = []
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="可变 ModelScope master"):
        validate_executable_plan(plan)


def test_comfyui_master_allows_serialized_position_file_downloads() -> None:
    plan = ready_comfyui_plan()
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    revision = download["command"].index("--revision")
    download["command"][revision + 1] = "master"
    files = download["command"][8:]
    download["command"] = download["command"][:8] + [files[0]]
    download["step_id"] = "download-1"
    download["name"] = "download first approved file"
    downloads = [download]
    for index, file in enumerate(files[1:], start=2):
        serial = deepcopy(download)
        serial["step_id"] = f"download-{index}"
        serial["sequence"] = 20 + index
        serial["name"] = f"download approved file {index}"
        serial["command"] = serial["command"][:8] + [file]
        serial["depends_on"] = [downloads[-1]["step_id"]]
        plan["steps"].append(serial)
        downloads.append(serial)
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 90,
        "name": "verify ModelScope asset integrity",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": [item["step_id"] for item in downloads],
        "success_criteria": ["all SHA-256 values match the observed master manifest"],
        "rollback_step_ids": [],
    }
    link = next(step for step in plan["steps"] if step["action"] == "create_service_config")
    link["depends_on"].append("verify-model-assets")
    plan["steps"].append(checksum)
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)


def test_comfyui_recovery_reuses_verified_model_assets_before_restart() -> None:
    plan = ready_comfyui_plan()
    plan["framework"]["runtime_artifact"]["reuse_verified_model_assets"] = True
    plan["steps"] = [
        step
        for step in plan["steps"]
        if step["action"] not in {"download_model", "create_service_config"}
    ]
    files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 20,
        "name": "verify reused ModelScope assets",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": [],
        "success_criteria": ["all reused assets match the official manifest"],
        "rollback_step_ids": [],
    }
    model_link = {
        "step_id": "verify-model-link",
        "sequence": 21,
        "name": "verify reused models link",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["test", "-e", "/opt/h3/models"],
        "depends_on": ["verify-model-assets"],
        "success_criteria": ["models link remains available"],
        "rollback_step_ids": [],
    }
    start = next(step for step in plan["steps"] if step["action"] == "start_own_service")
    start["depends_on"] = ["systemd-ready", "verify-model-assets", "verify-model-link"]
    plan["steps"].extend([checksum, model_link])
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)


def test_comfyui_execution_blocks_without_reviewed_runtime_paths() -> None:
    plan = ready_comfyui_plan()
    request = deployment_request()
    request["target"]["install_root"] = "/opt/h3"
    request["target"]["model_root"] = "/opt/h3/model"
    request["target"]["gpu_ids"] = [0]
    request["model"]["variant"] = "both"
    request["framework_preference"] = "comfyui"
    request["service"] = {
        "mode": "managed_service",
        "bind_host": "0.0.0.0",
        "port": 8188,
        "max_concurrency": 1,
    }
    request["inference"] = {
        "concurrency": 1,
        "short_edge": 768,
        "duration_seconds": 4,
        "input_authorization_reference": "fixture-reference-input-auth",
    }
    del plan["environment"]["bootstrap_uv"]
    with pytest.raises(ExecutionBlocked, match="独立 uv 与 Python 路径"):
        authorize_execution(plan, request, comfy_host())


def test_comfyui_uses_reviewed_runtime_when_ssh_path_hides_uv() -> None:
    plan = ready_comfyui_plan()
    request = deployment_request()
    request["target"]["install_root"] = "/opt/h3"
    request["target"]["model_root"] = "/opt/h3/model"
    request["target"]["gpu_ids"] = [0]
    request["model"]["variant"] = "both"
    request["framework_preference"] = "comfyui"
    request["service"] = {
        "mode": "managed_service",
        "bind_host": "0.0.0.0",
        "port": 8188,
        "max_concurrency": 1,
    }
    request["inference"] = {
        "concurrency": 1,
        "short_edge": 768,
        "duration_seconds": 4,
        "input_authorization_reference": "fixture-reference-input-auth",
    }
    host = comfy_host()
    host["software"]["uv_version"] = "bash: uv: command not found"
    host["software"]["python"] = [{"executable": "python3", "version": "Python 3.8.10"}]
    assert authorize_execution(plan, request, host).status == "PASS"


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
    deployment_record = json.loads(deployment_record_path.read_text(encoding="utf-8"))
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

    record = json.loads((tmp_path / "deployments" / "dep-1.json").read_text(encoding="utf-8"))
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
        {"gpu_id": "GPU-h200-0", "pid": 88, "process_name": "unrelated", "memory_bytes": 1}
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
            "docker",
            "run",
            "--name",
            "dep-1",
            "--gpus",
            "device=0,1,2,3",
            "--publish",
            "127.0.0.1:30011:30011",
            "--detach",
            image,
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


def test_uncatalogued_profile_must_not_bypass_adaptation_research() -> None:
    plan = ready_plan()
    plan["compatibility"]["profile_id"] = "made-up-recommended-profile"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)

    with pytest.raises(ExecutionBlocked, match="必须先进入适配调研"):
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
