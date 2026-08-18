"""需求与已观测主机门禁。本模块绝不修改主机。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts._common import (
    HarnessError,
    canonical_plan_sha256,
    file_sha256,
    load_document,
    validate_instance,
)
from scripts.intake import discovery_missing_fields
from scripts.verify_service import (
    _validate_inference_proof,
    _validate_semantic_review,
    validate_media,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GateResult:
    status: str
    blockers: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    next_stage: str | None = None
    plan_conditions: tuple[str, ...] = ()
    selected_candidate_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "blockers": list(self.blockers),
            "missing_fields": list(self.missing_fields),
            "warnings": list(self.warnings),
            "recommendations": list(self.recommendations),
            "next_stage": self.next_stage,
            "plan_conditions": list(self.plan_conditions),
            "selected_candidate_id": self.selected_candidate_id,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _at_path(document: Mapping[str, Any], dotted: str) -> object | None:
    current: object = document
    for component in dotted.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return None
        current = current[component]
    return current


def requirement_gate(
    request: Mapping[str, Any],
    *,
    policy_path: Path = ROOT / "config/harness-policy.yaml",
    schema_path: Path = ROOT / "schemas/deployment-request.schema.json",
) -> GateResult:
    policy = _load_yaml(policy_path)
    required = policy["requirement_gate"]["required_user_intent"]
    missing = tuple(path for path in required if _at_path(request, path) is None)
    if missing:
        return GateResult("NEEDS_USER_INPUT", missing_fields=missing)
    try:
        validate_instance(dict(request), schema_path.name)
    except HarnessError as exc:
        return GateResult("BLOCKED", blockers=(str(exc),))
    return GateResult("PASS")


def discovery_gate(source: Mapping[str, Any]) -> GateResult:
    """只检查只读 SSH 发现所需的连接意图，不授权任何远程写入。"""
    missing = discovery_missing_fields(source)
    if missing:
        return GateResult("NEEDS_USER_INPUT", missing_fields=missing)
    return GateResult(
        "PASS",
        recommendations=("连接时必须继续使用严格的 SSH known_hosts 主机指纹校验",),
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def _adaptation_blocker(category: str, description: str) -> str:
    labels = {
        "license": "许可条件",
        "safety": "安全门禁",
        "physical_capacity": "物理容量",
        "unavailable_target": "目标资源不可用",
        "protected_change_required": "需要受保护的系统变更",
        "unverifiable": "无法完成必要验证",
    }
    return f"{labels.get(category, category)}无法通过适配绕过：{description}"


def _resolve_artifact(reference: Mapping[str, Any], artifact_root: Path) -> Path:
    path = Path(str(reference["path"]))
    resolved = path.resolve() if path.is_absolute() else (artifact_root / path).resolve()
    if not resolved.is_relative_to(artifact_root.resolve()) or not resolved.is_file():
        raise HarnessError(f"制品缺失或越出允许目录：{reference['path']}")
    if file_sha256(resolved).lower() != str(reference["sha256"]).lower():
        raise HarnessError(f"制品哈希不匹配：{reference['path']}")
    return resolved


def _validated_artifact(
    reference: Mapping[str, Any], artifact_root: Path, schema_name: str
) -> dict[str, Any]:
    path = _resolve_artifact(reference, artifact_root)
    document = load_document(path)
    validate_instance(document, schema_name)
    return document


def _validate_host_binding(assessment: Mapping[str, Any], artifact_root: Path) -> None:
    request = _validated_artifact(
        assessment["request_artifact"], artifact_root, "deployment-request.schema.json"
    )
    host = _validated_artifact(
        assessment["host_profile_artifact"], artifact_root, "host-profile.schema.json"
    )
    request_host = request["target"]["host"]
    if (
        request["request_id"] != assessment["request_id"]
        or request_host.get("host_id") != assessment["host_id"]
        or host["host_id"] != assessment["host_id"]
        or host["observed_at"] != assessment["host_profile_observed_at"]
        or host.get("probe", {}).get("status") != "COMPLETE"
    ):
        raise HarnessError("适配评估未绑定完整且精确的当前主机观测")


def _validate_mechanisms(
    candidate: Mapping[str, Any],
    gap_ids: set[str],
    evidence: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    candidate_id = str(candidate["candidate_id"])
    if not gap_ids.issubset(set(candidate["applies_to_gap_ids"])):
        raise HarnessError(f"候选方案 {candidate_id} 没有覆盖全部适配缺口")
    if not set(candidate["evidence_ids"]).issubset(evidence):
        raise HarnessError(f"候选方案 {candidate_id} 引用了不存在的证据")
    mechanism_covered: set[str] = set()
    for mechanism in candidate["mitigation_mechanisms"]:
        mechanism_id = mechanism["mechanism_id"]
        if not set(mechanism["addresses_gap_ids"]).issubset(gap_ids):
            raise HarnessError(f"适配机制 {mechanism_id} 引用了不存在的缺口")
        if not set(mechanism["evidence_ids"]).issubset(candidate["evidence_ids"]):
            raise HarnessError(f"适配机制 {mechanism_id} 的证据未归入候选方案")
        for evidence_id in mechanism["evidence_ids"]:
            item = evidence[evidence_id]
            if (
                item["source"]["authority_tier"] in {"S", "A"}
                and item["confidence"] == "HIGH"
                and item["officially_verified"]
                and not item["inference"]
                and set(mechanism["addresses_gap_ids"]).issubset(
                    set(item.get("supports_gap_ids", []))
                )
                and mechanism_id in item.get("supports_mechanism_ids", [])
            ):
                mechanism_covered.update(mechanism["addresses_gap_ids"])
    return mechanism_covered


def _candidate_common_rejection(
    candidate: Mapping[str, Any], gap_ids: set[str], evidence: Mapping[str, Mapping[str, Any]]
) -> str | None:
    candidate_id = str(candidate["candidate_id"])
    try:
        _validate_mechanisms(candidate, gap_ids, evidence)
    except HarnessError as exc:
        return str(exc)
    failed_checks = [
        check["name"] for check in candidate["applicability_checks"] if check["status"] != "PASS"
    ]
    if failed_checks:
        return f"候选方案 {candidate_id} 与目标条件不匹配：{'、'.join(failed_checks)}"
    return None


def _validate_trial_evidence(
    assessment: Mapping[str, Any],
    candidate: Mapping[str, Any],
    artifact_root: Path,
    media_validator: Callable[[Path, dict[str, Any]], tuple[bool, str]] | None = None,
) -> bool:
    trial = candidate["local_reproduction"]["trial_evidence"]
    request = _validated_artifact(
        assessment["request_artifact"], artifact_root, "deployment-request.schema.json"
    )
    plan = _validated_artifact(trial["trial_plan"], artifact_root, "deployment-plan.schema.json")
    execution = _validated_artifact(
        trial["execution_record"], artifact_root, "execution-record.schema.json"
    )
    inference_proof = _validated_artifact(
        trial["inference_proof"], artifact_root, "inference-proof.schema.json"
    )
    semantic_review = _validated_artifact(
        trial["semantic_review"], artifact_root, "semantic-review.schema.json"
    )
    verification = _validated_artifact(
        trial["verification_result"], artifact_root, "verification-result.schema.json"
    )
    plan_hash = canonical_plan_sha256(plan)
    if (
        plan.get("purpose") != "CAPACITY_TRIAL"
        or plan["status"] != "READY"
        or plan["review"]["status"] != "APPROVED"
        or plan["review"]["plan_sha256"] != plan_hash
        or plan["license_gate"]["status"] != "PASS"
        or plan["request_id"] != assessment["request_id"]
        or plan["target"]["host_id"] != assessment["host_id"]
        or plan["target"]["gpu_ids"] != request["target"]["gpu_ids"]
        or plan["model"]["id"] != request["model"]["id"]
        or plan["model"]["variant"] != request["model"]["variant"]
        or plan["host_profile_observed_at"] != assessment["host_profile_observed_at"]
        or plan["deployment_id"] != trial["trial_deployment_id"]
    ):
        raise HarnessError("容量试跑计划未通过独立 READY 审核或未绑定当前请求/主机")
    assessment_evidence = {item["evidence_id"]: item for item in assessment["evidence"]}
    plan_evidence = {item["evidence_id"]: item for item in plan["evidence"]}
    if any(
        plan_evidence.get(evidence_id) != assessment_evidence.get(evidence_id)
        for evidence_id in candidate["evidence_ids"]
    ):
        raise HarnessError("容量试跑计划没有原样绑定候选方案使用的研究证据")
    if (
        execution["deployment_id"] != plan["deployment_id"]
        or execution["host_id"] != plan["target"]["host_id"]
        or execution["plan_sha256"] != plan_hash
        or execution["status"] != "EXECUTED"
    ):
        raise HarnessError("容量试跑执行记录与已审核计划不一致")
    planned_step_ids = [step["step_id"] for step in plan["steps"]]
    executed_step_ids = [step["step_id"] for step in execution["steps"]]
    if executed_step_ids != planned_step_ids or any(
        step["returncode"] != 0 for step in execution["steps"]
    ):
        raise HarnessError("容量试跑执行记录未按顺序成功覆盖已审核计划的全部步骤")
    if (
        verification["deployment_id"] != plan["deployment_id"]
        or verification["host_id"] != plan["target"]["host_id"]
        or verification["plan_sha256"] != plan_hash
        or verification["framework"]["name"] != plan["framework"]["name"]
        or verification["framework"]["version"] != plan["framework"]["version"]
        or trial["checked_at"] != verification["completed_at"]
    ):
        raise HarnessError("容量试跑验证结果与已审核计划不一致")
    for reference in verification["artifacts"]:
        _resolve_artifact(reference, artifact_root)
    for level in verification["levels"].values():
        for reference in level.get("evidence", []):
            _resolve_artifact(reference, artifact_root)
    output_reference = inference_proof["output"]
    output_path = _resolve_artifact(output_reference, artifact_root)
    recipe_path = (ROOT / plan["verification"]["recipe_ref"]).resolve()
    if not recipe_path.is_relative_to((ROOT / "models").resolve()) or not recipe_path.is_file():
        raise HarnessError("容量试跑验证配方缺失或越出模型目录")
    recipe = load_document(recipe_path)
    duration = _validate_inference_proof(inference_proof, plan, output_path, recipe)
    _validate_semantic_review(semantic_review, plan, output_path)
    technical_pass, technical_detail = (media_validator or validate_media)(output_path, recipe)
    if not technical_pass:
        raise HarnessError(f"容量试跑输出未通过可信媒体验证：{technical_detail}")
    required_artifacts = {
        str(trial["inference_proof"]["sha256"]).lower(),
        str(trial["semantic_review"]["sha256"]).lower(),
        str(output_reference["sha256"]).lower(),
    }
    recorded_artifacts = {str(item["sha256"]).lower() for item in verification["artifacts"]}
    if not required_artifacts.issubset(recorded_artifacts):
        raise HarnessError("验证结果未原样包含推理证明、语义审核和生成输出")
    if verification["metrics"]["generation_duration_seconds"] != duration:
        raise HarnessError("验证耗时未由推理证明中的提交/完成时间重建")
    verified = verification["overall_status"] == "VERIFIED"
    declared = candidate["local_reproduction"]["status"]
    if (declared == "PASS") != verified:
        raise HarnessError("复现结论与结构化验证结果不一致")
    return verified


def assess_compatibility_adaptation(
    assessment: Mapping[str, Any],
    *,
    policy_path: Path = ROOT / "config/harness-policy.yaml",
    schema_path: Path = ROOT / "schemas/compatibility-assessment.schema.json",
    artifact_root: Path = ROOT,
    media_validator: Callable[[Path, dict[str, Any]], tuple[bool, str]] | None = None,
) -> GateResult:
    """验证适配状态及制品链；结果不替代正式部署的其他门禁。"""
    try:
        validate_instance(dict(assessment), schema_path.name)
        _validate_host_binding(assessment, artifact_root)
    except HarnessError as exc:
        return GateResult("BLOCKED", blockers=(f"适配评估制品无效：{exc}",))
    policy = _load_yaml(policy_path)["compatibility_adaptation"]
    adaptable = set(policy["adaptable_gap_categories"])
    hard = set(policy["hard_gate_categories"])
    gaps = assessment["gaps"]
    hard_gaps = [gap for gap in gaps if gap["category"] in hard]
    if hard_gaps:
        return GateResult(
            "BLOCKED",
            blockers=tuple(
                _adaptation_blocker(gap["category"], gap["description"]) for gap in hard_gaps
            ),
        )
    adaptable_gap_ids = {gap["gap_id"] for gap in gaps if gap["category"] in adaptable}
    research = assessment["research"]
    if research["status"] != "COMPLETED":
        result = GateResult(
            "RESEARCH_NEEDED",
            warnings=tuple(gap["description"] for gap in gaps),
            recommendations=(
                "硬件不在官方推荐配置中，不等于不能部署；先进入调研，寻找候选适配方案并在目标主机验证。",
            ),
            next_stage="RESEARCH",
        )
        return _consistent_assessment_status(assessment, result)
    candidates = research["candidates"]
    if not candidates:
        result = GateResult(
            "BLOCKED",
            blockers=("调研已完成，但没有找到覆盖当前硬件缺口且可验证的候选方案。",),
        )
        return _consistent_assessment_status(assessment, result)
    evidence = {item["evidence_id"]: item for item in assessment["evidence"]}
    selected_id = research.get("selected_candidate_id")
    selected = next((item for item in candidates if item["candidate_id"] == selected_id), None)
    if selected is None:
        result = GateResult("BLOCKED", blockers=("调研完成后必须明确选择一个候选方案。",))
        return _consistent_assessment_status(assessment, result)
    rejection = _candidate_common_rejection(selected, adaptable_gap_ids, evidence)
    if rejection:
        result = GateResult("BLOCKED", blockers=(rejection,))
        return _consistent_assessment_status(assessment, result)
    mechanism_coverage = _validate_mechanisms(selected, adaptable_gap_ids, evidence)
    if not adaptable_gap_ids.issubset(mechanism_coverage):
        result = GateResult(
            "RESEARCH_NEEDED",
            warnings=("当前候选仍只是社区复现线索，不能进入容量试跑。",),
            recommendations=("继续查找直接支持具体缺口与缓解机制的 S/A 级上游证据。",),
            next_stage="RESEARCH",
            selected_candidate_id=selected_id,
        )
        return _consistent_assessment_status(assessment, result)
    reproduction = selected["local_reproduction"]
    if reproduction["status"] == "NOT_RUN":
        result = GateResult(
            "READY_FOR_TRIAL",
            recommendations=("先生成并审核一份 purpose=CAPACITY_TRIAL 的独立 READY 计划。",),
            next_stage="PLAN",
            plan_conditions=tuple(selected["plan_conditions"]),
            selected_candidate_id=selected_id,
        )
        return _consistent_assessment_status(assessment, result)
    try:
        verified = _validate_trial_evidence(assessment, selected, artifact_root, media_validator)
    except HarnessError as exc:
        result = GateResult("BLOCKED", blockers=(str(exc),))
        return _consistent_assessment_status(assessment, result)
    if not verified:
        result = GateResult("BLOCKED", blockers=("目标主机容量试跑已经失败。",))
        return _consistent_assessment_status(assessment, result)
    result = GateResult(
        "VALIDATED",
        warnings=("适配证据已验证；完整需求、许可、计划证据和审核仍须分别通过。",),
        recommendations=("可将适配评估作为正式计划输入，但它本身不授权规划或执行。",),
        next_stage="PLAN",
        plan_conditions=tuple(selected["plan_conditions"]),
        selected_candidate_id=selected_id,
    )
    return _consistent_assessment_status(assessment, result)


def _consistent_assessment_status(assessment: Mapping[str, Any], result: GateResult) -> GateResult:
    if (
        assessment["adaptation_status"] != result.status
        or assessment["next_stage"] != result.next_stage
    ):
        blockers = result.blockers + ("适配评估中声明的状态与经制品校验得到的状态不一致。",)
        return GateResult(
            "BLOCKED",
            blockers=blockers,
        )
    return result


validate_assessment_artifacts = assess_compatibility_adaptation


def host_preflight(
    request: Mapping[str, Any],
    host_profile: Mapping[str, Any],
    *,
    required_cuda: str | None = None,
    environment_strategy: str | None = None,
    environment_isolated: bool = False,
) -> GateResult:
    gate = requirement_gate(request)
    if not gate.passed:
        return gate
    blockers: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []
    probe = host_profile.get("probe", {})
    if not isinstance(probe, Mapping) or probe.get("status") == "FAILED":
        blockers.append("SSH 主机发现失败；禁止执行")
    elif probe.get("status") != "COMPLETE":
        blockers.append("主机发现尚未完成")
    target = request["target"]
    requested_host_id = target["host"].get("host_id")
    if requested_host_id and requested_host_id != host_profile.get("host_id"):
        blockers.append("观测到的 host_id 与请求的 host_id 不一致")
    gpu_ids = target["gpu_ids"]
    hardware = host_profile.get("hardware", {})
    observed_gpus = hardware.get("gpus", []) if isinstance(hardware, Mapping) else []
    available_ids = {gpu["index"] for gpu in observed_gpus} | {gpu["uuid"] for gpu in observed_gpus}
    missing_gpus = [gpu_id for gpu_id in gpu_ids if gpu_id not in available_ids]
    if missing_gpus:
        blockers.append(f"请求的 GPU ID 不存在：{missing_gpus}")
    runtime = host_profile.get("runtime", {})
    processes = runtime.get("gpu_processes", []) if isinstance(runtime, Mapping) else []
    occupied = []
    uuid_to_index = {gpu["uuid"]: gpu["index"] for gpu in observed_gpus}
    for process in processes:
        process_gpu = process.get("gpu_id")
        if process_gpu in gpu_ids or uuid_to_index.get(process_gpu) in gpu_ids:
            occupied.append(
                {
                    "gpu_id": process_gpu,
                    "pid": process.get("pid"),
                    "process_name": process.get("process_name"),
                }
            )
    if occupied:
        blockers.append(f"请求的 GPU 正被占用：{occupied}；未停止任何进程")
    network = host_profile.get("network", {})
    listening = network.get("listening_ports", []) if isinstance(network, Mapping) else []
    if request["service"]["port"] in listening:
        blockers.append(f"请求的端口 {request['service']['port']} 已在监听")
    nvidia = host_profile.get("software", {}).get("nvidia", {})
    observed_cuda = nvidia.get("cuda_compatibility") if isinstance(nvidia, Mapping) else None
    if required_cuda and (
        not observed_cuda or _version_tuple(observed_cuda) < _version_tuple(required_cuda)
    ):
        detail = f"CUDA 兼容版本 {observed_cuda or '未知'} 未满足 {required_cuda}"
        blockers.append(detail)
        recommendations.append(
            "请选择 NVIDIA 驱动满足已审核运行时要求的主机；容器或 venv 隔离无法修复不兼容的主机驱动"
        )
    if (
        request["existing_environment_policy"] == "PRESERVE_AND_ISOLATE"
        and not environment_isolated
    ):
        blockers.append("请求要求环境隔离")
        recommendations.append("请使用独立容器或 venv")
    return GateResult(
        "BLOCKED" if blockers else "PASS",
        tuple(blockers),
        warnings=tuple(warnings),
        recommendations=tuple(recommendations),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行分层需求门禁和主机兼容性预检")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discovery = subparsers.add_parser("discovery", help="检查是否可以开始只读 SSH 侦察")
    discovery_source = discovery.add_mutually_exclusive_group(required=True)
    discovery_source.add_argument("--draft", type=Path)
    discovery_source.add_argument("--request", type=Path)
    requirement = subparsers.add_parser("requirement")
    requirement.add_argument("--request", required=True, type=Path)
    host = subparsers.add_parser("host")
    host.add_argument("--request", required=True, type=Path)
    host.add_argument("--host-profile", required=True, type=Path)
    host.add_argument("--required-cuda")
    host.add_argument("--environment-strategy", choices=("container", "venv"), required=True)
    host.add_argument("--isolated", action="store_true")
    adaptation = subparsers.add_parser(
        "adaptation", help="判断兼容性缺口应进入调研、阻断还是允许规划"
    )
    adaptation.add_argument("--assessment", required=True, type=Path)
    adaptation.add_argument("--artifact-root", type=Path, default=ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "discovery":
            result = discovery_gate(load_document(args.draft or args.request))
        elif args.command == "adaptation":
            result = assess_compatibility_adaptation(
                load_document(args.assessment), artifact_root=args.artifact_root
            )
        else:
            request = load_document(args.request)
        if args.command == "requirement":
            result = requirement_gate(request)
        elif args.command == "host":
            result = host_preflight(
                request,
                load_document(args.host_profile),
                required_cuda=args.required_cuda,
                environment_strategy=args.environment_strategy,
                environment_isolated=args.isolated,
            )
        print(json.dumps(result.as_dict(), indent=2))
        return (
            0 if result.status in {"PASS", "RESEARCH_NEEDED", "READY_FOR_TRIAL", "VALIDATED"} else 3
        )
    except (HarnessError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
