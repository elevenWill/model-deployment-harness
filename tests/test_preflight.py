from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import yaml

from scripts._common import ROOT, canonical_plan_sha256, file_sha256
from scripts.preflight import (
    assess_compatibility_adaptation,
    discovery_gate,
    host_preflight,
    main,
    requirement_gate,
)
from scripts.run_inference import resolve_output_binding
from tests.test_remote_exec import deployment_request, observed_host, ready_plan


def request():
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
        "model": {"id": "example", "variant": "base"},
        "framework_preference": "sglang",
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "research",
        "deployment_region": "CN",
    }


def host_profile():
    return {
        "host_id": "knode25",
        "probe": {"status": "COMPLETE"},
        "hardware": {"gpus": [{"index": 0, "uuid": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]},
        "software": {"nvidia": {"cuda_compatibility": "12.4"}},
        "network": {"listening_ports": [22]},
        "runtime": {"gpu_processes": []},
    }


def test_missing_intent_is_needs_user_input_and_never_defaulted():
    result = requirement_gate({"target": {"host": {"address": "10.0.0.25"}}})
    assert result.status == "NEEDS_USER_INPUT"
    assert "target.gpu_ids" in result.missing_fields


def test_read_only_discovery_is_not_blocked_by_complete_deployment_intent():
    source = {
        "target": {"host": {"address": "10.0.0.25", "ssh_username": "deploy", "ssh_port": 22}}
    }
    assert discovery_gate(source).status == "PASS"
    assert requirement_gate(source).status == "NEEDS_USER_INPUT"


def test_ssh_failure_blocks():
    profile = host_profile()
    profile["probe"]["status"] = "FAILED"
    assert (
        host_preflight(
            request(), profile, environment_strategy="container", environment_isolated=True
        ).status
        == "BLOCKED"
    )


def test_gpu_occupation_blocks_without_killing():
    profile = host_profile()
    profile["runtime"]["gpu_processes"] = [
        {
            "gpu_id": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "pid": 99,
            "process_name": "training",
        }
    ]
    result = host_preflight(
        request(), profile, environment_strategy="container", environment_isolated=True
    )
    assert result.status == "BLOCKED" and "未停止任何进程" in " ".join(result.blockers)


def test_cuda_mismatch_and_occupied_port_remain_execution_blockers():
    cuda = host_preflight(
        request(),
        host_profile(),
        required_cuda="12.6",
        environment_strategy="container",
        environment_isolated=True,
    )
    profile = host_profile()
    profile["network"]["listening_ports"].append(30011)
    port = host_preflight(
        request(), profile, environment_strategy="container", environment_isolated=True
    )
    assert cuda.status == port.status == "BLOCKED"


def _write(path: Path, value: dict) -> dict[str, str]:
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": path.name, "sha256": file_sha256(path)}


def _evidence() -> list[dict]:
    return [
        {
            "evidence_id": "official-mechanism",
            "source": {
                "title": "上游低显存机制",
                "url": "https://example.com/upstream-low-vram",
                "authority_tier": "A",
                "publisher": "Framework",
            },
            "retrieved_at": "2026-08-17T00:00:00Z",
            "claim": "固定运行时直接提供低显存机制",
            "applies_to": ["example/base"],
            "confidence": "HIGH",
            "officially_verified": True,
            "inference": False,
            "supports_gap_ids": ["gpu-profile"],
            "supports_mechanism_ids": ["low-vram"],
        }
    ]


def _assessment(
    tmp_path: Path,
    *,
    status: str,
    reproduction: dict,
    research_status: str = "COMPLETED",
    category: str = "recommended_profile_mismatch",
) -> dict:
    request_ref = _write(tmp_path / "request.json", deployment_request())
    host_ref = _write(tmp_path / "host.json", observed_host())
    candidates = (
        []
        if research_status != "COMPLETED"
        else [
            {
                "candidate_id": "low-vram-candidate",
                "description": "低显存试验",
                "applies_to_gap_ids": ["gpu-profile"],
                "evidence_ids": ["official-mechanism"],
                "mitigation_mechanisms": [
                    {
                        "mechanism_id": "low-vram",
                        "description": "降低峰值显存",
                        "addresses_gap_ids": ["gpu-profile"],
                        "evidence_ids": ["official-mechanism"],
                    }
                ],
                "applicability_checks": [{"name": "目标条件", "status": "PASS"}],
                "local_reproduction": reproduction,
                "plan_conditions": ["仅单并发"],
            }
        ]
    )
    research = {"status": research_status, "candidates": candidates}
    if candidates:
        research["selected_candidate_id"] = "low-vram-candidate"
    return {
        "schema_version": "1.0",
        "assessment_id": "adapt-1",
        "request_id": "req-1",
        "request_artifact": request_ref,
        "host_id": "knode25",
        "host_profile_observed_at": observed_host()["observed_at"],
        "host_profile_artifact": host_ref,
        "adaptation_status": status,
        "next_stage": None
        if status == "BLOCKED"
        else ("RESEARCH" if status == "RESEARCH_NEEDED" else "PLAN"),
        "gaps": [
            {"gap_id": "gpu-profile", "category": category, "description": "目标配置不在推荐范围"}
        ],
        "research": research,
        "evidence": _evidence(),
    }


def test_non_recommended_hardware_routes_to_research_and_cli_succeeds(tmp_path, capsys):
    assessment = _assessment(
        tmp_path,
        status="RESEARCH_NEEDED",
        reproduction={"status": "NOT_RUN"},
        research_status="NOT_STARTED",
    )
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(assessment), encoding="utf-8")
    result = assess_compatibility_adaptation(assessment, artifact_root=tmp_path)
    assert result.status == "RESEARCH_NEEDED" and result.next_stage == "RESEARCH"
    assert main(["adaptation", "--assessment", str(path), "--artifact-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "RESEARCH_NEEDED"


def test_not_run_routes_to_separately_reviewed_capacity_trial(tmp_path):
    assessment = _assessment(tmp_path, status="READY_FOR_TRIAL", reproduction={"status": "NOT_RUN"})
    result = assess_compatibility_adaptation(assessment, artifact_root=tmp_path)
    assert result.status == "READY_FOR_TRIAL" and result.next_stage == "PLAN"
    assert "CAPACITY_TRIAL" in result.recommendations[0]


def test_unrelated_official_source_cannot_turn_untried_community_lead_into_validation(
    tmp_path,
):
    assessment = _assessment(tmp_path, status="RESEARCH_NEEDED", reproduction={"status": "NOT_RUN"})
    assessment["evidence"][0]["source"]["authority_tier"] = "C"
    assessment["evidence"][0]["officially_verified"] = False
    assessment["evidence"][0]["confidence"] = "MEDIUM"
    unrelated = {
        **_evidence()[0],
        "evidence_id": "official-unrelated",
        "source": {
            "title": "无关官方安装页",
            "url": "https://example.com/official-install",
            "authority_tier": "A",
            "publisher": "Framework",
        },
        "supports_gap_ids": ["some-other-gap"],
        "supports_mechanism_ids": ["some-other-mechanism"],
    }
    assessment["evidence"].append(unrelated)
    candidate = assessment["research"]["candidates"][0]
    candidate["evidence_ids"].append("official-unrelated")
    candidate["mitigation_mechanisms"][0]["evidence_ids"].append("official-unrelated")

    result = assess_compatibility_adaptation(assessment, artifact_root=tmp_path)

    assert result.status == "RESEARCH_NEEDED"
    assert "社区复现线索" in result.warnings[0]


def _add_trial_chain(tmp_path: Path, assessment: dict, *, verified: bool) -> None:
    pretrial_assessment = deepcopy(assessment)
    pretrial_assessment["adaptation_status"] = "READY_FOR_TRIAL"
    pretrial_assessment["next_stage"] = "PLAN"
    pretrial_assessment["research"]["candidates"][0]["local_reproduction"] = {"status": "NOT_RUN"}
    pretrial_ref = _write(tmp_path / "pretrial-assessment.json", pretrial_assessment)
    plan = ready_plan()
    plan["purpose"] = "CAPACITY_TRIAL"
    plan["compatibility"]["basis"] = "CAPACITY_TRIAL"
    plan["compatibility"]["adaptation"] = {
        "assessment_ref": pretrial_ref,
        "assessment_id": "adapt-1",
        "candidate_id": "low-vram-candidate",
        "plan_conditions": [{"condition": "仅单并发", "preflight_step_id": "capacity-condition"}],
    }
    plan["steps"][0]["sequence"] = 2
    plan["steps"][0]["depends_on"] = ["capacity-condition"]
    plan["steps"].insert(
        0,
        {
            "step_id": "capacity-condition",
            "sequence": 1,
            "name": "check trial condition",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["test", "-d", "/opt/models"],
            "depends_on": [],
            "success_criteria": ["condition remains true"],
            "rollback_step_ids": [],
        },
    )
    plan["evidence"].extend(assessment["evidence"])
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    plan_ref = _write(tmp_path / "trial-plan.json", plan)
    plan_hash = plan["review"]["plan_sha256"]
    execution = {
        "schema_version": "1.0",
        "producer": "HARNESS_PLAN_EXECUTOR",
        "execution_id": "exec-trial",
        "deployment_id": "dep-1",
        "host_id": "knode25",
        "plan_sha256": plan_hash,
        "started_at": "2026-08-17T00:03:00Z",
        "completed_at": "2026-08-17T00:04:00Z",
        "status": "EXECUTED",
        "steps": [
            {
                "step_id": step["step_id"],
                "started_at": "2026-08-17T00:03:00Z",
                "completed_at": "2026-08-17T00:04:00Z",
                "returncode": 0,
                "stdout_redacted": "ok",
                "stderr_redacted": "",
            }
            for step in plan["steps"]
        ],
    }
    execution_ref = _write(tmp_path / "execution.json", execution)
    payload_ref = _write(tmp_path / "payload.json", {"prompt": "fixture"})
    payload_ref["path"] = str(tmp_path / "payload.json")
    payload_ref["media_type"] = "application/json"
    output_path = tmp_path / "output.mp4"
    output_path.write_bytes(b"fixture media bytes")
    output_ref = {
        "path": str(output_path),
        "sha256": file_sha256(output_path),
        "media_type": "video/mp4",
    }
    response_document = {
        "id": "job-1",
        "status": "COMPLETED",
        "output": {
            "id": "artifact-1",
            "url": "/outputs/artifact-1",
            "sha256": output_ref["sha256"],
        },
    }
    response_ref = _write(tmp_path / "response.json", response_document)
    response_ref["path"] = str(tmp_path / "response.json")
    response_ref["media_type"] = "application/json"
    inference_api = yaml.safe_load(
        (ROOT / "models/minimax-h3/verify.yaml").read_text()
    )["inference_api"]
    proof = {
        "schema_version": "1.0",
        "producer": "HARNESS_HTTP_RUNNER",
        "deployment_id": "dep-1",
        "plan_sha256": plan_hash,
        "endpoint": "http://127.0.0.1:30011",
        "request": {
            "method": "POST",
            "path": "/v1/videos",
            "payload": payload_ref,
            "submitted_at": "2026-08-17T00:04:00Z",
        },
        "job": {
            "job_id": "job-1",
            "status": "COMPLETED",
            "completed_at": "2026-08-17T00:04:01Z",
            "response": response_ref,
            "runtime_error": None,
        },
        "output_binding": resolve_output_binding(
            response_document, "job-1", inference_api, "http://127.0.0.1:30011/"
        )
        | {
            "downloaded_at": "2026-08-17T00:04:02Z",
            "download_sha256": output_ref["sha256"],
            "download_content_length": str(output_path.stat().st_size),
            "response_headers": {"content_length": str(output_path.stat().st_size)},
        },
        "output": output_ref,
    }
    proof_ref = _write(tmp_path / "proof.json", proof)
    semantic = {
        "schema_version": "1.0",
        "review_id": "semantic-1",
        "deployment_id": "dep-1",
        "plan_sha256": plan_hash,
        "output_sha256": output_ref["sha256"],
        "reviewed_by": "fixture-reviewer",
        "reviewed_at": "2026-08-17T00:05:00Z",
        "checks": {
            "not_blank_or_frozen": "PASS",
            "audio_present_not_silent": "PASS",
            "task_alignment": "PASS",
        },
    }
    semantic_ref = _write(tmp_path / "semantic.json", semantic)

    def check(state: str) -> dict:
        return {
            "status": state,
            "checked_at": "2026-08-17T00:05:00Z",
            "detail": "trial",
            **({"evidence": [output_ref]} if state == "PASS" else {}),
        }

    final_state = "PASS" if verified else "FAIL"
    verification = {
        "schema_version": "1.0",
        "verification_id": "verify-trial",
        "deployment_id": "dep-1",
        "host_id": "knode25",
        "plan_sha256": plan_hash,
        "recipe_ref": "models/minimax-h3/verify.yaml",
        "started_at": "2026-08-17T00:04:00Z",
        "completed_at": "2026-08-17T00:05:00Z",
        "levels": {
            "L1_environment": check("PASS"),
            "L2_process": check("PASS"),
            "L3_port": check("PASS"),
            "L4_api": check("PASS"),
            "L5_real_inference": check(final_state),
            "L6_output_validation": check(final_state),
        },
        "overall_status": "VERIFIED" if verified else "FAILED",
        "metrics": {"generation_duration_seconds": 1.0 if verified else None},
        "framework": {"name": "sglang", "version": plan["framework"]["version"]},
        "gpu_topology_summary": "fixture",
        "command_redacted": [],
        "artifacts": [proof_ref, semantic_ref, output_ref],
    }
    verification_ref = _write(tmp_path / "verification.json", verification)
    candidate = assessment["research"]["candidates"][0]
    candidate["local_reproduction"] = {
        "status": "PASS" if verified else "FAIL",
        "trial_evidence": {
            "checked_at": "2026-08-17T00:05:00Z",
            "trial_deployment_id": "dep-1",
            "trial_plan": plan_ref,
            "execution_record": execution_ref,
            "inference_proof": proof_ref,
            "semantic_review": semantic_ref,
            "verification_result": verification_ref,
        },
    }


def test_real_hashed_trial_chain_validates_adaptation_but_not_full_plan(tmp_path):
    assessment = _assessment(tmp_path, status="VALIDATED", reproduction={"status": "NOT_RUN"})
    _add_trial_chain(tmp_path, assessment, verified=True)
    result = assess_compatibility_adaptation(
        assessment, artifact_root=tmp_path, media_validator=lambda *_: (True, "fixture")
    )
    assert result.status == "VALIDATED" and result.next_stage == "PLAN"
    assert not hasattr(result, "plan_allowed")
    assert "仍须分别通过" in result.warnings[0]


def test_trial_rejects_empty_execution_steps_and_self_reported_media_pass(tmp_path):
    assessment = _assessment(tmp_path, status="VALIDATED", reproduction={"status": "NOT_RUN"})
    _add_trial_chain(tmp_path, assessment, verified=True)
    trial = assessment["research"]["candidates"][0]["local_reproduction"]["trial_evidence"]
    execution_path = tmp_path / "execution.json"
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    execution["steps"] = []
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    trial["execution_record"]["sha256"] = file_sha256(execution_path)

    empty = assess_compatibility_adaptation(
        assessment, artifact_root=tmp_path, media_validator=lambda *_: (True, "fixture")
    )
    assert empty.status == "BLOCKED"
    assert "全部步骤" in empty.blockers[0]

    _add_trial_chain(tmp_path, assessment, verified=True)
    arbitrary_media = assess_compatibility_adaptation(assessment, artifact_root=tmp_path)
    assert arbitrary_media.status == "BLOCKED"
    assert "媒体验证" in arbitrary_media.blockers[0]


def test_trial_rejects_verification_pass_without_typed_inference_proof(tmp_path):
    assessment = _assessment(tmp_path, status="VALIDATED", reproduction={"status": "NOT_RUN"})
    _add_trial_chain(tmp_path, assessment, verified=True)
    trial = assessment["research"]["candidates"][0]["local_reproduction"]["trial_evidence"]
    del trial["inference_proof"]

    result = assess_compatibility_adaptation(
        assessment, artifact_root=tmp_path, media_validator=lambda *_: (True, "fixture")
    )

    assert result.status == "BLOCKED"
    assert "模式校验失败" in result.blockers[0]


def test_tampered_trial_artifact_and_failed_trial_block(tmp_path):
    assessment = _assessment(tmp_path, status="BLOCKED", reproduction={"status": "NOT_RUN"})
    _add_trial_chain(tmp_path, assessment, verified=False)
    assert (
        assess_compatibility_adaptation(
            assessment,
            artifact_root=tmp_path,
            media_validator=lambda *_: (False, "fixture failed"),
        ).status
        == "BLOCKED"
    )
    (tmp_path / "execution.json").write_text("{}", encoding="utf-8")
    result = assess_compatibility_adaptation(assessment, artifact_root=tmp_path)
    assert result.status == "BLOCKED" and "哈希" in result.blockers[0]


def test_hard_gate_still_blocks_and_categories_match_policy_schema(tmp_path):
    assessment = _assessment(
        tmp_path, status="BLOCKED", reproduction={"status": "NOT_RUN"}, category="physical_capacity"
    )
    assert (
        "物理容量"
        in assess_compatibility_adaptation(assessment, artifact_root=tmp_path).blockers[0]
    )
    policy = yaml.safe_load((ROOT / "config/harness-policy.yaml").read_text(encoding="utf-8"))[
        "compatibility_adaptation"
    ]
    schema = json.loads(
        (ROOT / "schemas/compatibility-assessment.schema.json").read_text(encoding="utf-8")
    )
    assert set(schema["$defs"]["gapCategory"]["enum"]) == set(
        policy["adaptable_gap_categories"]
    ) | set(policy["hard_gate_categories"])
