"""以失败即阻断方式执行模式有效且已审核的 READY 部署计划。"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from scripts._common import (
    HarnessError,
    canonical_plan_sha256,
    file_sha256,
    load_document,
    validate_instance,
)
from scripts.deployment_archive import DeploymentArchive
from scripts.preflight import GateResult, host_preflight, validate_assessment_artifacts
from scripts.probe_host import (
    CommandResult,
    CommandTransport,
    ParamikoTransport,
    collect_host_profile,
    secret_environment,
)

ROOT = Path(__file__).resolve().parents[1]


class ExecutionBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class StepResult:
    step_id: str
    started_at: str
    completed_at: str
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    steps: tuple[StepResult, ...]
    blocker: str | None = None


def _policy(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _resolved_repo_file(reference: str, expected_parent: Path) -> Path:
    candidate = (ROOT / reference).resolve()
    if not candidate.is_relative_to(expected_parent.resolve()) or not candidate.is_file():
        raise ExecutionBlocked(f"仓库制品无效或缺失：{reference}")
    return candidate


def _immutable_version(value: str) -> bool:
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{7,40}", value) or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value)
    )


def _validate_lifecycle(plan: Mapping[str, Any]) -> None:
    required = ["INTAKE", "REQUIREMENT_GATE", "HOST_DISCOVERY", "RESEARCH", "PLAN", "PLAN_REVIEW"]
    stages = [item.get("stage") for item in plan.get("lifecycle", {}).get("transitions", [])]
    if stages != required:
        raise ExecutionBlocked(
            "READY 计划必须包含完全一致的已完成生命周期前缀：" + " -> ".join(required)
        )
    seen_paths: set[Path] = set()
    sources: dict[str, dict[str, Any]] = {}
    for transition in plan["lifecycle"]["transitions"]:
        reference = transition["artifact"]
        path = Path(reference["path"])
        resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
        if not resolved.is_relative_to(ROOT.resolve()) or not resolved.is_file():
            raise ExecutionBlocked(f"生命周期制品缺失或位于仓库外：{reference['path']}")
        if file_sha256(resolved).lower() != reference["sha256"].lower():
            raise ExecutionBlocked(f"生命周期制品哈希不匹配：{reference['path']}")
        if resolved in seen_paths:
            raise ExecutionBlocked("每个生命周期阶段都需要独立制品")
        seen_paths.add(resolved)
        try:
            artifact = json.loads(resolved.read_text(encoding="utf-8"))
            validate_instance(artifact, "lifecycle-artifact.schema.json")
        except (HarnessError, OSError, json.JSONDecodeError) as exc:
            raise ExecutionBlocked(f"阶段 {transition['stage']} 的生命周期制品无效") from exc
        if (
            artifact["stage"] != transition["stage"]
            or artifact["request_id"] != plan["request_id"]
            or artifact["deployment_id"] != plan["deployment_id"]
        ):
            raise ExecutionBlocked("生命周期制品未绑定到其阶段、请求和计划")
        source_ref = artifact["source_artifact"]
        source_path = (ROOT / source_ref["path"]).resolve()
        if (
            not source_path.is_relative_to(ROOT.resolve())
            or not source_path.is_file()
            or file_sha256(source_path).lower() != source_ref["sha256"].lower()
        ):
            raise ExecutionBlocked("生命周期源制品缺失或哈希错误")
        try:
            source = json.loads(source_path.read_text(encoding="utf-8"))
            validate_instance(source, "lifecycle-stage-source.schema.json")
        except (HarnessError, OSError, json.JSONDecodeError) as exc:
            raise ExecutionBlocked("生命周期源制品未通过其类型化契约") from exc
        if (
            source["stage"] != transition["stage"]
            or source["request_id"] != plan["request_id"]
            or source["deployment_id"] != plan["deployment_id"]
        ):
            raise ExecutionBlocked("生命周期源未绑定到其阶段、请求和计划")
        sources[transition["stage"]] = source
    host_source = sources["HOST_DISCOVERY"]
    if (
        host_source["host_id"] != plan["target"]["host_id"]
        or host_source["observed_at"] != plan["host_profile_observed_at"]
    ):
        raise ExecutionBlocked("主机发现源与已审核主机档案不一致")
    evidence_ids = {item["evidence_id"] for item in plan["evidence"]}
    if not set(sources["RESEARCH"]["evidence_ids"]).issubset(evidence_ids):
        raise ExecutionBlocked("研究生命周期源存在悬挂证据 ID")
    if sources["PLAN"]["plan_sha256"] != sources["PLAN_REVIEW"]["reviewed_plan_sha256"]:
        raise ExecutionBlocked("计划审核源未引用计划草案哈希")


def _transitive_dependencies(step_id: str, steps: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """返回全部传递依赖，遇到悬挂引用或循环时失败关闭。"""
    pending = list(steps[step_id].get("depends_on", []))
    dependencies: set[str] = set()
    while pending:
        dependency = pending.pop()
        if dependency == step_id:
            raise ExecutionBlocked("执行步骤依赖形成循环")
        if dependency in dependencies:
            continue
        referenced = steps.get(dependency)
        if referenced is None:
            raise ExecutionBlocked(f"执行步骤存在悬挂依赖：{dependency}")
        dependencies.add(dependency)
        pending.extend(referenced.get("depends_on", []))
    return dependencies


def _validate_adaptation_binding(plan: Mapping[str, Any]) -> None:
    """把适配计划绑定到不可变评估及写入前检查。"""
    adaptation = plan["compatibility"].get("adaptation")
    if adaptation is None:
        return
    reference = adaptation["assessment_ref"]
    assessment_path = _resolved_repo_file(reference["path"], ROOT)
    if not hmac.compare_digest(file_sha256(assessment_path).lower(), reference["sha256"].lower()):
        raise ExecutionBlocked("兼容性评估制品 SHA-256 不匹配")
    try:
        assessment = load_document(assessment_path)
        validate_instance(assessment, "compatibility-assessment.schema.json")
    except (HarnessError, OSError, ValueError) as exc:
        raise ExecutionBlocked("兼容性评估制品无效") from exc
    if (
        assessment.get("assessment_id") != adaptation["assessment_id"]
        or assessment.get("request_id") != plan["request_id"]
        or assessment.get("host_id") != plan["target"]["host_id"]
        or assessment.get("host_profile_observed_at") != plan["host_profile_observed_at"]
    ):
        raise ExecutionBlocked("兼容性评估未绑定到当前请求、主机和主机观测")
    basis = plan["compatibility"]["basis"]
    expected_status = "READY_FOR_TRIAL" if basis == "CAPACITY_TRIAL" else "VALIDATED"
    assessment_gate = validate_assessment_artifacts(assessment, artifact_root=ROOT)
    if assessment_gate.status != expected_status or assessment_gate.next_stage != "PLAN":
        detail = "；".join(assessment_gate.blockers)
        raise ExecutionBlocked(
            f"{basis} 计划要求状态为 {expected_status} 的兼容性评估"
            + (f"：{detail}" if detail else "")
        )
    request_ref = assessment["request_artifact"]
    request_path = _resolved_repo_file(request_ref["path"], ROOT)
    if not hmac.compare_digest(file_sha256(request_path).lower(), request_ref["sha256"].lower()):
        raise ExecutionBlocked("适配评估的请求制品 SHA-256 不匹配")
    try:
        assessed_request = load_document(request_path)
        validate_instance(assessed_request, "deployment-request.schema.json")
    except (HarnessError, OSError, ValueError) as exc:
        raise ExecutionBlocked("适配评估的请求制品无效") from exc
    if (
        assessed_request["request_id"] != plan["request_id"]
        or assessed_request["target"]["gpu_ids"] != plan["target"]["gpu_ids"]
        or assessed_request["model"]["id"] != plan["model"]["id"]
        or assessed_request["model"]["variant"] != plan["model"]["variant"]
        or assessed_request["framework_preference"] != plan["framework"]["name"]
    ):
        raise ExecutionBlocked("计划未绑定适配评估使用的精确请求")
    research = assessment.get("research", {})
    if research.get("selected_candidate_id") != adaptation["candidate_id"]:
        raise ExecutionBlocked("计划引用的适配候选与评估选择不一致")
    candidates = {item.get("candidate_id"): item for item in research.get("candidates", [])}
    candidate = candidates.get(adaptation["candidate_id"])
    if candidate is None:
        raise ExecutionBlocked("计划引用了不存在的适配候选")
    reproduction = candidate.get("local_reproduction", {})
    trial_plan: Mapping[str, Any] | None = None
    if basis == "CAPACITY_TRIAL":
        if reproduction.get("status") != "NOT_RUN" or "trial_evidence" in reproduction:
            raise ExecutionBlocked("容量试跑只能绑定尚未执行的候选方案")
    else:
        if reproduction.get("status") != "PASS":
            raise ExecutionBlocked("适配候选缺少通过的目标主机试运行实证")
        trial_evidence = reproduction.get("trial_evidence", {})
        trial_plan_ref = trial_evidence.get("trial_plan", {})
        if not isinstance(trial_plan_ref, Mapping):
            raise ExecutionBlocked("适配候选缺少试运行计划引用")
        trial_plan_path = _resolved_repo_file(str(trial_plan_ref.get("path", "")), ROOT)
        if not hmac.compare_digest(
            file_sha256(trial_plan_path).lower(), str(trial_plan_ref.get("sha256", "")).lower()
        ):
            raise ExecutionBlocked("适配试运行计划 SHA-256 不匹配")
        try:
            trial_plan = load_document(trial_plan_path)
        except (HarnessError, OSError, ValueError) as exc:
            raise ExecutionBlocked("适配试运行计划无法读取") from exc
        if (
            trial_plan.get("request_id") != plan["request_id"]
            or trial_plan.get("deployment_id") != trial_evidence.get("trial_deployment_id")
            or trial_plan.get("target", {}).get("host_id") != plan["target"]["host_id"]
            or trial_plan.get("target", {}).get("gpu_ids") != plan["target"]["gpu_ids"]
            or trial_plan.get("host_profile_observed_at") != plan["host_profile_observed_at"]
            or trial_plan.get("model", {}).get("id") != plan["model"]["id"]
            or trial_plan.get("model", {}).get("variant") != plan["model"]["variant"]
            or trial_plan.get("framework", {}).get("name") != plan["framework"]["name"]
            or trial_plan.get("framework", {}).get("version") != plan["framework"]["version"]
        ):
            raise ExecutionBlocked("适配试运行未绑定到当前请求、GPU、模型、主机观测和不可变运行时")
    assessment_evidence = {item["evidence_id"]: item for item in assessment.get("evidence", [])}
    plan_evidence = {item["evidence_id"]: item for item in plan["evidence"]}
    trial_plan_evidence = (
        {item["evidence_id"]: item for item in trial_plan.get("evidence", [])}
        if trial_plan is not None
        else assessment_evidence
    )
    for evidence_id in candidate.get("evidence_ids", []):
        if (
            evidence_id not in assessment_evidence
            or plan_evidence.get(evidence_id) != assessment_evidence[evidence_id]
            or trial_plan_evidence.get(evidence_id) != assessment_evidence[evidence_id]
        ):
            raise ExecutionBlocked("适配候选证据未被试运行和正式计划完整、原样引用")
    bindings = adaptation["plan_conditions"]
    if [item["condition"] for item in bindings] != candidate.get("plan_conditions"):
        raise ExecutionBlocked("计划条件与已验证适配候选不一致")
    condition_step_ids = [item["preflight_step_id"] for item in bindings]
    if len(condition_step_ids) != len(set(condition_step_ids)):
        raise ExecutionBlocked("每条适配计划条件必须绑定独立的执行前检查")
    steps = {step["step_id"]: step for step in plan["steps"]}
    for step_id in condition_step_ids:
        step = steps.get(step_id)
        if step is None or step["action"] != "inspect" or step["action_class"] != "READ_ONLY":
            raise ExecutionBlocked("适配计划条件必须绑定只读 inspect 步骤")
    required_checks = set(condition_step_ids)
    for step in plan["steps"]:
        dependencies = _transitive_dependencies(step["step_id"], steps)
        if step["action_class"] != "READ_ONLY" and not required_checks.issubset(dependencies):
            raise ExecutionBlocked("每个远程写步骤都必须依赖全部适配执行前检查")


def _validate_compatibility_basis(plan: Mapping[str, Any]) -> None:
    """阻止未登记硬件档案绕过适配调研与审核。"""
    compatibility = plan["compatibility"]
    basis = compatibility["basis"]
    if basis in {"VALIDATED_ADAPTATION", "CAPACITY_TRIAL"} and "adaptation" not in compatibility:
        raise ExecutionBlocked("适配或容量试跑必须绑定 CompatibilityAssessment")
    if basis != "CATALOG_PROFILE":
        return
    _catalog_profile(plan)
    _validate_catalog_limits(plan)


def _catalog_profile(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    manifest_path = _resolved_repo_file(plan["model"]["recipe_ref"], ROOT / "models")
    catalog_path = manifest_path.parent / "compatibility.yaml"
    if not catalog_path.is_file():
        raise ExecutionBlocked("模型缺少兼容性档案，必须先进入适配调研")
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    profiles = {item.get("id"): item for item in catalog.get("profiles", [])}
    profile = profiles.get(plan["compatibility"]["profile_id"])
    if profile is None or profile.get("framework") != plan["framework"]["name"]:
        raise ExecutionBlocked("目标配置不在模型兼容性档案中，必须先进入适配调研")
    return profile


def _validate_catalog_limits(plan: Mapping[str, Any]) -> None:
    profile = _catalog_profile(plan)
    limits = profile.get("limits")
    expected_fields = {
        "max_concurrency",
        "max_short_edge",
        "max_duration_seconds",
        "allowed_variants",
        "input_authorization",
    }
    if not isinstance(limits, Mapping) or set(limits) != expected_fields:
        raise ExecutionBlocked("目录推理限制缺失或包含未知字段，必须先进入适配调研")
    authorization = limits["input_authorization"]
    if not isinstance(authorization, Mapping) or set(authorization) != {
        "required_variants",
        "input_kind",
    }:
        raise ExecutionBlocked("目录输入授权限制无法机器验证，必须先进入适配调研")
    binding = plan["compatibility"].get("catalog_limits")
    if not isinstance(binding, Mapping):
        raise ExecutionBlocked("目录计划缺少精确 service/inference 限制绑定")
    if (
        binding.get("max_concurrency") != limits["max_concurrency"]
        or binding.get("max_short_edge") != limits["max_short_edge"]
        or binding.get("max_duration_seconds") != limits["max_duration_seconds"]
        or binding.get("variant") != plan["model"]["variant"]
        or binding.get("variant") not in limits["allowed_variants"]
    ):
        raise ExecutionBlocked("计划绑定的目录推理限制或模型变体不一致")
    if plan["service"].get("max_concurrency") != binding.get("selected_concurrency"):
        raise ExecutionBlocked("服务并发配置未绑定目录限制")
    selected = (
        ("selected_concurrency", "max_concurrency"),
        ("selected_short_edge", "max_short_edge"),
        ("selected_duration_seconds", "max_duration_seconds"),
    )
    if any(
        not isinstance(binding.get(selected_field), (int, float))
        or binding[selected_field] > limits[maximum_field]
        for selected_field, maximum_field in selected
    ):
        raise ExecutionBlocked("计划的 service/inference 参数超过目录限制")
    requires_authorization = plan["model"]["variant"] in authorization["required_variants"]
    expected_input_kind = authorization["input_kind"] if requires_authorization else "none"
    reference = binding.get("input_authorization_reference")
    if binding.get("input_kind") != expected_input_kind or (
        requires_authorization and not isinstance(reference, str)
    ):
        raise ExecutionBlocked("计划未绑定目录要求的参考输入授权")
    if not requires_authorization and reference is not None:
        raise ExecutionBlocked("无需参考输入的变体不得携带无关授权引用")


def _validate_catalog_request_limits(
    plan: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    if plan["compatibility"]["basis"] != "CATALOG_PROFILE":
        return
    _validate_catalog_limits(plan)
    inference = request.get("inference")
    binding = plan["compatibility"]["catalog_limits"]
    if not isinstance(inference, Mapping) or (
        inference.get("concurrency") != binding["selected_concurrency"]
        or inference.get("short_edge") != binding["selected_short_edge"]
        or inference.get("duration_seconds") != binding["selected_duration_seconds"]
        or inference.get("input_authorization_reference")
        != binding.get("input_authorization_reference")
    ):
        raise ExecutionBlocked("目录计划未绑定请求中的精确推理范围")
    reference = binding.get("input_authorization_reference")
    if reference is not None and plan["license_gate"].get("authorization_reference") != reference:
        raise ExecutionBlocked("参考输入授权未同时绑定请求、许可门禁和计划")


def _validate_catalog_profile_host(
    plan: Mapping[str, Any], host_profile: Mapping[str, Any]
) -> None:
    """将实时选中 GPU 与目录中的每项明确条件逐一比较。"""
    if plan["compatibility"]["basis"] != "CATALOG_PROFILE":
        return
    profile = _catalog_profile(plan)
    gpu_requirement = profile.get("gpu")
    topology_requirement = profile.get("topology")
    if not isinstance(gpu_requirement, Mapping) or not isinstance(topology_requirement, Mapping):
        raise ExecutionBlocked("兼容性档案缺少明确 GPU 或拓扑条件，必须先进入适配调研")
    if set(gpu_requirement) - {"model", "count", "memory_gib_each"} or set(
        topology_requirement
    ) - {"kind", "allowed_link_prefixes"}:
        raise ExecutionBlocked("目录包含执行器无法验证的物理条件，必须先进入适配调研")
    all_gpus = host_profile.get("hardware", {}).get("gpus", [])
    aliases: dict[object, Mapping[str, Any]] = {}
    for gpu in all_gpus:
        aliases[gpu["index"]] = gpu
        aliases[gpu["uuid"]] = gpu
    try:
        selected = [aliases[gpu_id] for gpu_id in plan["target"]["gpu_ids"]]
    except KeyError as exc:
        raise ExecutionBlocked("目录兼容性检查找不到计划选择的 GPU") from exc
    expected_count = gpu_requirement.get("count")
    expected_model = str(gpu_requirement.get("model", ""))
    expected_memory = gpu_requirement.get("memory_gib_each")
    if (
        not isinstance(expected_count, int)
        or not isinstance(expected_memory, (int, float))
        or len(selected) != expected_count
    ):
        raise ExecutionBlocked("目标 GPU 数量与目录兼容性档案不一致")
    expected_model_token = re.sub(r"[^A-Z0-9]", "", expected_model.upper())
    if not expected_model_token or any(
        expected_model_token not in re.sub(r"[^A-Z0-9]", "", str(gpu["model"]).upper())
        for gpu in selected
    ):
        raise ExecutionBlocked("目标 GPU 型号与目录兼容性档案不一致")
    minimum_bytes = int(float(expected_memory) * 1024**3)
    if any(gpu["memory_total_bytes"] < minimum_bytes for gpu in selected):
        raise ExecutionBlocked("目标 GPU 显存低于目录兼容性档案要求")
    host_ram = profile.get("host_ram")
    if host_ram is not None:
        if not isinstance(host_ram, Mapping) or set(host_ram) - {
            "minimum_available_gib",
            "recommended_total_gib",
        }:
            raise ExecutionBlocked("目录包含执行器无法验证的主机内存条件，必须先进入适配调研")
        minimum_available_gib = host_ram.get("minimum_available_gib")
        memory = host_profile.get("hardware", {}).get("memory")
        if (
            not isinstance(minimum_available_gib, (int, float))
            or minimum_available_gib < 0
            or not isinstance(memory, Mapping)
            or not isinstance(memory.get("available_bytes"), int)
        ):
            raise ExecutionBlocked("主机观测缺少目录要求的可用内存事实，必须先进入适配调研")
        required_available_bytes = int(float(minimum_available_gib) * 1024**3)
        if memory["available_bytes"] < required_available_bytes:
            raise ExecutionBlocked("主机可用内存低于目录兼容性档案要求，必须先进入适配调研")
    topology_kind = topology_requirement.get("kind")
    allowed_prefixes = topology_requirement.get("allowed_link_prefixes")
    if topology_kind == "SINGLE_GPU":
        if expected_count != 1 or allowed_prefixes != []:
            raise ExecutionBlocked("单 GPU 目录拓扑条件无效")
        return
    if (
        topology_kind != "FULL_MESH"
        or not isinstance(allowed_prefixes, list)
        or not allowed_prefixes
    ):
        raise ExecutionBlocked("多 GPU 目录拓扑条件无效，必须先进入适配调研")
    position_by_alias: dict[object, int] = {}
    for position, gpu in enumerate(selected):
        position_by_alias[gpu["index"]] = position
        position_by_alias[gpu["uuid"]] = position
    observed_pairs: dict[frozenset[int], str] = {}
    for link in host_profile.get("hardware", {}).get("gpu_topology", []):
        a = position_by_alias.get(link["gpu_a"])
        b = position_by_alias.get(link["gpu_b"])
        if a is not None and b is not None and a != b:
            observed_pairs[frozenset((a, b))] = str(link["link"])
    expected_pairs = {
        frozenset((a, b)) for a in range(expected_count) for b in range(a + 1, expected_count)
    }
    if set(observed_pairs) != expected_pairs or any(
        not any(link.startswith(str(prefix)) for prefix in allowed_prefixes)
        for link in observed_pairs.values()
    ):
        raise ExecutionBlocked("目标 GPU 拓扑与目录兼容性档案不一致")


def _validate_capacity_trial_actions(plan: Mapping[str, Any]) -> None:
    if plan["purpose"] != "CAPACITY_TRIAL":
        return
    allowed = {
        "inspect",
        "create_target_directory",
        "create_isolated_venv",
        "stage_source_bundle",
        "clone_source_checkout",
        "checkout_source_revision",
        "install_isolated_dependencies",
        "pull_container",
        "download_model",
        "create_service_config",
        "start_own_service",
        "stop_own_service",
    }
    actions = {
        step["action"]
        for collection in (plan["steps"], plan["rollback"]["steps"])
        for step in collection
    }
    if not actions.issubset(allowed):
        raise ExecutionBlocked("容量试跑包含超出隔离部署与自有服务范围的动作")


def _validate_framework_evidence_and_recipe(plan: Mapping[str, Any]) -> dict[str, Any]:
    framework = plan["framework"]
    if not _immutable_version(framework["version"]):
        raise ExecutionBlocked("框架版本必须为不可变提交或镜像摘要")
    recipe_path = _resolved_repo_file(framework["recipe_ref"], ROOT / "models")
    recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    if recipe.get("framework", {}).get("name") != framework["name"]:
        raise ExecutionBlocked("框架配方名称与所选框架不一致")
    recipe_pin = recipe.get("framework", {}).get("deployment_pin")
    if not recipe_pin or not _immutable_version(recipe_pin):
        raise ExecutionBlocked("框架配方缺少已解析的不可变部署 pin")
    if framework["version"] != recipe_pin:
        raise ExecutionBlocked("框架 pin 与已审核框架配方不一致")
    runtime = framework["runtime_artifact"]
    expected_probe = ["git", "-C", runtime["location"], "rev-parse", "HEAD"]
    if runtime["revision"] != framework["version"] or runtime["probe_command"] != expected_probe:
        raise ExecutionBlocked("运行时制品探测/修订版本与框架 pin 不一致")
    if not _under_remote_root(runtime["location"], (plan["target"]["install_root"],)):
        raise ExecutionBlocked("框架运行时 checkout 越出 install_root")
    executable_name = {"sglang": "sglang", "vllm-omni": "vllm"}.get(framework["name"])
    if framework["name"] == "comfyui":
        expected_executable = str(
            PurePosixPath(plan["target"]["install_root"]) / ".venv/bin/python"
        )
    elif executable_name is not None:
        expected_executable = str(
            PurePosixPath(runtime["location"]) / ".venv/bin" / executable_name
        )
    else:
        raise ExecutionBlocked("框架没有已实现的可执行文件绑定")
    if runtime["executable"] != expected_executable:
        raise ExecutionBlocked("框架可执行文件未绑定到已审核 checkout")
    source_bundle = runtime.get("source_bundle")
    if source_bundle is not None:
        source_tree = recipe.get("framework", {}).get("source_tree")
        if (
            not isinstance(source_tree, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", source_tree)
            or source_bundle["tree"].lower() != source_tree.lower()
            or not _under_remote_root(
                source_bundle["remote_path"], (plan["target"]["install_root"],)
            )
            or source_bundle["remote_path"] == runtime["location"]
        ):
            raise ExecutionBlocked("本地暂存源码包必须绑定配方固定 tree 且位于 install_root")
        local_bundle = Path(source_bundle["local_path"])
        if (
            not local_bundle.is_file()
            or file_sha256(local_bundle).lower() != source_bundle["sha256"].lower()
        ):
            raise ExecutionBlocked("本地暂存源码包缺失或 SHA-256 不匹配")
    evidence = {item["evidence_id"]: item for item in plan["evidence"]}
    for evidence_id in framework["evidence_ids"]:
        item = evidence.get(evidence_id)
        if item is None:
            raise ExecutionBlocked(f"框架证据引用悬挂：{evidence_id}")
        if item["source"]["authority_tier"] not in {"S", "A"}:
            raise ExecutionBlocked("关键框架决策缺少 S/A 级证据")
        if item["confidence"] != "HIGH" or item["inference"]:
            raise ExecutionBlocked("关键框架证据必须具有高置信度且为直接证据")
    return recipe


def _model_manifest(plan: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = _resolved_repo_file(plan["model"]["recipe_ref"], ROOT / "models")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model", {}).get("id") != plan["model"]["id"]:
        raise ExecutionBlocked("模型清单 ID 与计划不一致")
    return manifest


def _comfy_bootstrap(plan: Mapping[str, Any]) -> tuple[str, str]:
    """Return the reviewed, user-owned uv/Python pair for an isolated ComfyUI venv."""
    environment = plan["environment"]
    uv_path = environment.get("bootstrap_uv")
    python_path = environment.get("bootstrap_python")
    if not isinstance(uv_path, str) or not isinstance(python_path, str):
        raise ExecutionBlocked("ComfyUI 计划必须记录已观测的独立 uv 与 Python 路径")
    if (
        not PurePosixPath(uv_path).is_absolute()
        or PurePosixPath(uv_path).name != "uv"
        or not PurePosixPath(python_path).is_absolute()
        or not PurePosixPath(python_path).name.startswith("python")
    ):
        raise ExecutionBlocked("ComfyUI 独立 uv/Python 路径无效")
    return uv_path, python_path


def _comfy_dependency_argv(
    framework_recipe: Mapping[str, Any], plan: Mapping[str, Any]
) -> tuple[str, ...]:
    """Expand the reviewed ComfyUI dependency argv without using a shell."""
    template = framework_recipe.get("runtime", {}).get("dependency_argv")
    if not isinstance(template, list) or not all(isinstance(item, str) for item in template):
        raise ExecutionBlocked("ComfyUI 配方缺少固定的隔离依赖安装 argv")
    uv_path, _python_path = _comfy_bootstrap(plan)
    values = {
        "bootstrap_uv": uv_path,
        "venv_python": str(PurePosixPath(plan["target"]["install_root"]) / ".venv/bin/python"),
        "runtime_root": plan["framework"]["runtime_artifact"]["location"],
    }
    try:
        return tuple(item.format(**values) for item in template)
    except KeyError as exc:
        raise ExecutionBlocked("ComfyUI 依赖 argv 包含未知占位符") from exc


def _scope_authorizes(step: Mapping[str, Any]) -> bool:
    approval = step.get("approval")
    if not isinstance(approval, Mapping) or approval.get("status") != "APPROVED":
        return False
    scope = approval.get("scope", "")
    if not isinstance(scope, str):
        return False
    tokens = set(re.split(r"[\s,;]+", scope))
    return step["step_id"] in tokens and step["action"] in tokens


def _under_remote_root(value: str, roots: Sequence[str]) -> bool:
    path = PurePosixPath(value)
    return path.is_absolute() and any(
        path == PurePosixPath(root) or path.is_relative_to(root) for root in roots
    )


def _argument_after(argv: Sequence[str], flag: str) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _validate_step_command(
    step: Mapping[str, Any],
    plan: Mapping[str, Any],
    command_policy: Mapping[str, Any],
    framework_recipe: Mapping[str, Any],
) -> None:
    argv = tuple(step["command"])
    if not argv or any("\x00" in value for value in argv):
        raise ExecutionBlocked(f"{step['step_id']} 的 argv 无效")
    executable = PurePosixPath(argv[0]).name
    if executable in set(command_policy["shell_interpreters_forbidden"]):
        raise ExecutionBlocked(f"{step['step_id']} 禁止使用 shell 解释器")
    allowed = set(command_policy.get(step["action"], []))
    if executable not in allowed:
        raise ExecutionBlocked(f"命令可执行文件与声明动作 {step['action']} 不一致：{executable}")
    roots = (plan["target"]["install_root"], plan["target"]["model_root"])
    action = step["action"]
    if action == "inspect":
        if executable == "test":
            comfy_bootstrap_paths: tuple[str, str] = ()
            if plan["framework"]["name"] == "comfyui":
                comfy_bootstrap_paths = _comfy_bootstrap(plan)
            allowed_paths = (*roots, *comfy_bootstrap_paths)
            normal_test = (
                len(argv) == 3
                and argv[1] in {"-d", "-e", "-f", "-x"}
                and any(_under_remote_root(argv[2], (root,)) for root in allowed_paths)
            )
            absent_source_bundle = (
                len(argv) == 4
                and tuple(argv[1:3]) == ("!", "-e")
                and isinstance(plan["framework"]["runtime_artifact"].get("source_bundle"), Mapping)
                and argv[3] == plan["framework"]["runtime_artifact"]["source_bundle"]["remote_path"]
            )
            if not normal_test and not absent_source_bundle:
                raise ExecutionBlocked("计划 inspect 步骤仅限对目标根目录执行 test")
        elif executable == "sha256sum":
            bundle = plan["framework"]["runtime_artifact"].get("source_bundle")
            if isinstance(bundle, Mapping) and argv == ("sha256sum", bundle["remote_path"]):
                pass
            else:
                manifest = _model_manifest(plan)
                assets = manifest.get("comfyui_assets", {}).get("master_integrity", {})
                files = tuple(
                    manifest.get("comfyui_assets", {})
                    .get("variants", {})
                    .get(plan["model"]["variant"], {})
                    .get("files", ())
                )
                expected = (
                    "sha256sum",
                    *(str(PurePosixPath(plan["target"]["model_root"]) / path) for path in files),
                )
                if argv != expected or not all(path in assets for path in files):
                    raise ExecutionBlocked("仅允许校验已审核的暂存源码包或 ModelScope 资产 SHA-256")
        elif executable == "git":
            runtime = plan["framework"]["runtime_artifact"]
            expected = (
                "git",
                "-C",
                runtime["location"],
                "rev-parse",
                "HEAD^{tree}",
            )
            source_tree = framework_recipe.get("framework", {}).get("source_tree")
            if not isinstance(source_tree, str) or argv != expected:
                raise ExecutionBlocked("仅允许校验已审核源码 checkout 的固定 tree")
        elif plan["framework"]["name"] != "comfyui" or argv != (
            "systemctl",
            "--user",
            "is-system-running",
        ):
            raise ExecutionBlocked("仅 ComfyUI 可以使用精确的只读 user systemd 可用性检查")
    elif action == "create_target_directory":
        paths = [value for value in argv[1:] if not value.startswith("-")]
        if executable != "mkdir" or "-p" not in argv or not paths:
            raise ExecutionBlocked("create_target_directory 需要 mkdir -p 与明确路径")
        if not all(_under_remote_root(value, roots) for value in paths):
            raise ExecutionBlocked("create_target_directory 越出已审核目标根目录")
    elif action == "create_isolated_venv":
        destination = argv[-1]
        valid_shape = (executable == "uv" and argv[1:2] == ("venv",)) or (
            executable in {"python", "python3"} and argv[1:3] == ("-m", "venv")
        )
        if plan["framework"]["name"] == "comfyui":
            uv_path, python_path = _comfy_bootstrap(plan)
            valid_shape = argv == (uv_path, "venv", "--python", python_path, destination)
        if not valid_shape or not _under_remote_root(
            destination, (plan["target"]["install_root"],)
        ):
            raise ExecutionBlocked("隔离 venv 的命令/路径与声明动作不一致")
    elif action == "stage_source_bundle":
        bundle = plan["framework"]["runtime_artifact"].get("source_bundle")
        expected = (
            (
                "sftp-upload",
                bundle["local_path"],
                bundle["remote_path"],
            )
            if isinstance(bundle, Mapping)
            else ()
        )
        if argv != expected:
            raise ExecutionBlocked("暂存源码包上传必须精确绑定已审核的本地与远端路径")
    elif action == "clone_source_checkout":
        runtime = plan["framework"]["runtime_artifact"]
        bundle = runtime.get("source_bundle")
        source = (
            bundle["remote_path"]
            if isinstance(bundle, Mapping)
            else framework_recipe["framework"]["source"]
        )
        expected = (
            "git",
            "clone",
            "--no-checkout",
            source,
            runtime["location"],
        )
        if argv != expected or not _under_remote_root(
            runtime["location"], (plan["target"]["install_root"],)
        ):
            raise ExecutionBlocked("源码 clone 必须使用配方允许的精确来源和 runtime 目录")
    elif action == "checkout_source_revision":
        runtime = plan["framework"]["runtime_artifact"]
        expected = ("git", "-C", runtime["location"], "checkout", "--detach", runtime["revision"])
        if argv != expected:
            raise ExecutionBlocked("源码 checkout 必须固定到已审核 runtime revision")
    elif action == "install_isolated_dependencies":
        runtime = plan["framework"]["runtime_artifact"]
        venv_python = str(PurePosixPath(plan["target"]["install_root"]) / ".venv/bin/python")
        expected = (
            _comfy_dependency_argv(framework_recipe, plan)
            if plan["framework"]["name"] == "comfyui"
            else (
                "uv",
                "pip",
                "install",
                "--python",
                venv_python,
                "-r",
                str(PurePosixPath(runtime["location"]) / "requirements.txt"),
                "modelscope==1.31.0",
            )
        )
        if argv != expected:
            raise ExecutionBlocked(
                "隔离依赖安装必须使用配方固定的 uv、requirements 和 ModelScope 客户端"
            )
    elif action == "pull_container":
        if (
            len(argv) != 3
            or argv[1] != "pull"
            or not re.search(r"@sha256:[0-9a-fA-F]{64}$", argv[2])
        ):
            raise ExecutionBlocked("拉取容器必须使用不可变镜像摘要")
    elif action == "download_model":
        revision = _argument_after(argv, "--revision")
        if executable == "hf":
            destination = _argument_after(argv, "--local-dir")
            if (
                argv[:2] != ("hf", "download")
                or not revision
                or not re.fullmatch(r"[0-9a-fA-F]{40}", revision)
            ):
                raise ExecutionBlocked("下载模型需要带 40 位十六进制修订版本的 hf download")
            if destination is None or not _under_remote_root(
                destination, (plan["target"]["model_root"],)
            ):
                raise ExecutionBlocked("模型下载目标越出 model_root")
        elif executable == "modelscope":
            manifest = _model_manifest(plan)
            source = manifest.get("download", {}).get("supported_sources", {}).get("modelscope", {})
            assets = (
                manifest.get("comfyui_assets", {})
                .get("variants", {})
                .get(plan["model"]["variant"], {})
            )
            destination = _argument_after(argv, "--local_dir")
            allowed_files = tuple(assets.get("files", ()))
            expected_prefix = (
                str(PurePosixPath(plan["target"]["install_root"]) / ".venv/bin/modelscope"),
                "download",
                "--model",
                source.get("repository"),
            )
            mutable_revision = source.get("mutable_revision")
            valid_revision = bool(revision and re.fullmatch(r"[0-9a-fA-F]{40}", revision))
            valid_revision = valid_revision or revision == mutable_revision == "master"
            if (
                argv[:4] != expected_prefix
                or not valid_revision
                or destination != plan["target"]["model_root"]
                or not _under_remote_root(destination, (plan["target"]["model_root"],))
                or tuple(argv[:8])
                != (*expected_prefix, "--revision", revision, "--local_dir", destination)
                or not tuple(argv[8:])
                or len(set(argv[8:])) != len(argv[8:])
                or not set(argv[8:]).issubset(set(allowed_files))
            ):
                raise ExecutionBlocked(
                    "ModelScope 下载必须使用精确仓库/revision和位置参数文件白名单"
                )
        else:  # pragma: no cover - command policy guards this branch
            raise ExecutionBlocked("未实现的模型下载客户端")
    elif action == "create_service_config":
        if plan["framework"]["name"] == "comfyui":
            expected = (
                "ln",
                "-s",
                plan["target"]["model_root"],
                str(PurePosixPath(plan["target"]["install_root"]) / "models"),
            )
            if argv != expected:
                raise ExecutionBlocked(
                    "ComfyUI 配置只能创建指向独立 model_root 的精确 models 符号链接"
                )
        else:
            non_options = [value for value in argv[1:] if not value.startswith("-")]
            if len(non_options) < 2 or not all(
                _under_remote_root(value, roots) for value in non_options[-2:]
            ):
                raise ExecutionBlocked("服务配置源/目标必须位于目标根目录下")
    elif action == "start_own_service":
        dangerous_prefixes = (
            "--privileged",
            "--pid",
            "--network",
            "--cap-add",
            "--device",
            "--volume",
            "--mount",
            "--host",
            "-v",
            "-H",
        )
        if executable in {"docker", "podman"} and any(
            argument == prefix or argument.startswith(prefix + "=")
            for argument in argv[1:]
            for prefix in dangerous_prefixes
        ):
            raise ExecutionBlocked("禁止危险的容器特权标志")
        expected = {
            "sglang": {"sglang", "docker", "podman"},
            "vllm-omni": {"vllm", "docker", "podman"},
            "comfyui": {"systemd-run"},
        }.get(plan["framework"]["name"], set())
        if executable not in expected:
            raise ExecutionBlocked("服务可执行文件与已审核框架不一致")
        if executable == "docker":
            image = argv[-1]
            gpu_selector = "device=" + ",".join(str(item) for item in plan["target"]["gpu_ids"])
            expected_publish = (
                f"{plan['service']['bind_host']}:{plan['service']['port']}:"
                f"{plan['service']['port']}"
            )
            expected_argv = (
                "docker",
                "run",
                "--name",
                plan["deployment_id"],
                "--gpus",
                gpu_selector,
                "--publish",
                expected_publish,
                "--detach",
                image,
            )
            if argv != expected_argv or not re.search(r"@sha256:[0-9a-fA-F]{64}$", image):
                raise ExecutionBlocked("Docker 服务启动必须符合已审核的精确 run 语法")
        elif executable == "podman":
            raise ExecutionBlocked("MVP 执行器尚未实现 Podman 服务启动")
        elif executable == "sglang":
            expected_argv = (
                plan["framework"]["runtime_artifact"]["executable"],
                "serve",
                "--model-path",
                plan["target"]["model_root"],
                "--model-variant",
                plan["model"]["variant"],
                "--num-gpus",
                str(len(plan["target"]["gpu_ids"])),
                "--host",
                plan["service"]["bind_host"],
                "--port",
                str(plan["service"]["port"]),
            )
            if argv != expected_argv:
                raise ExecutionBlocked("SGLang 服务命令与已审核启动命令不一致")
        elif executable == "vllm":
            expected_argv = (
                plan["framework"]["runtime_artifact"]["executable"],
                "serve",
                plan["target"]["model_root"],
                "--omni",
                "--trust-remote-code",
                "--num-gpus",
                str(len(plan["target"]["gpu_ids"])),
                "--host",
                plan["service"]["bind_host"],
                "--port",
                str(plan["service"]["port"]),
            )
            if argv != expected_argv:
                raise ExecutionBlocked("vLLM 服务命令与已审核启动命令不一致")
        elif executable == "systemd-run":
            runtime = plan["framework"]["runtime_artifact"]
            cuda_visible_devices = ",".join(str(item) for item in plan["target"]["gpu_ids"])
            expected_argv = (
                "systemd-run",
                "--user",
                f"--unit={plan['deployment_id']}",
                "--collect",
                f"--property=WorkingDirectory={runtime['location']}",
                f"--setenv=CUDA_VISIBLE_DEVICES={cuda_visible_devices}",
                runtime["executable"],
                str(PurePosixPath(runtime["location"]) / "main.py"),
                "--listen",
                plan["service"]["bind_host"],
                "--port",
                str(plan["service"]["port"]),
                "--base-directory",
                plan["target"]["install_root"],
                "--lowvram",
                "--disable-auto-launch",
            )
            if plan["framework"]["name"] != "comfyui" or argv != expected_argv:
                raise ExecutionBlocked("ComfyUI 服务必须通过隔离的精确 systemd-run --user 命令启动")
    elif action == "stop_own_service":
        expected = ("systemctl", "--user", "stop", f"{plan['deployment_id']}.service")
        if plan["framework"]["name"] != "comfyui" or argv != expected:
            raise ExecutionBlocked("回滚只能停止由当前部署创建的 ComfyUI user unit")


def _validate_comfyui_plan_shape(plan: Mapping[str, Any]) -> None:
    if plan["framework"]["name"] != "comfyui":
        return
    uv_path, python_path = _comfy_bootstrap(plan)
    steps = list(plan["steps"])
    action_set = {step["action"] for step in steps}
    required = {"inspect", "install_isolated_dependencies", "start_own_service"}
    runtime = plan["framework"]["runtime_artifact"]
    reusing_checkout = runtime.get("reuse_verified_checkout") is True
    reusing_model_assets = runtime.get("reuse_verified_model_assets") is True
    if not reusing_model_assets:
        required.update({"download_model", "create_service_config"})
    if not reusing_checkout:
        required.update(
            {"clone_source_checkout", "checkout_source_revision", "create_isolated_venv"}
        )
    if not required.issubset(action_set):
        raise ExecutionBlocked(
            "ComfyUI 计划缺少隔离运行时、ModelScope、user systemd 或服务启动步骤"
        )
    systemd_checks = {
        step["step_id"]
        for step in steps
        if tuple(step["command"]) == ("systemctl", "--user", "is-system-running")
    }
    starts = [step for step in steps if step["action"] == "start_own_service"]
    if (
        len(systemd_checks) != 1
        or len(starts) != 1
        or not systemd_checks.intersection(starts[0].get("depends_on", []))
    ):
        raise ExecutionBlocked("ComfyUI 启动必须依赖精确的 user systemd 只读可用性检查")
    bootstrap_checks = {
        step["step_id"]
        for step in steps
        if tuple(step["command"]) in {("test", "-x", uv_path), ("test", "-x", python_path)}
    }
    venv_steps = [step for step in steps if step["action"] == "create_isolated_venv"]
    if not reusing_checkout and (
        len(bootstrap_checks) != 2
        or len(venv_steps) != 1
        or not bootstrap_checks.issubset(venv_steps[0].get("depends_on", []))
    ):
        raise ExecutionBlocked("ComfyUI 创建隔离环境前必须检查已审核的 uv 与 Python 路径")
    if reusing_checkout:
        expected_tree_check = (
            "git",
            "-C",
            runtime["location"],
            "rev-parse",
            "HEAD^{tree}",
        )
        tree_steps = [step for step in steps if tuple(step["command"]) == expected_tree_check]
        venv_path = str(PurePosixPath(plan["target"]["install_root"]) / ".venv/bin/python")
        venv_checks = [
            step for step in steps if tuple(step["command"]) == ("test", "-x", venv_path)
        ]
        dependency_steps = [
            step for step in steps if step["action"] == "install_isolated_dependencies"
        ]
        if (
            len(tree_steps) != 1
            or len(venv_checks) != 1
            or len(dependency_steps) != 1
            or tree_steps[0]["step_id"] not in dependency_steps[0].get("depends_on", [])
            or venv_checks[0]["step_id"] not in dependency_steps[0].get("depends_on", [])
        ):
            raise ExecutionBlocked("复用 checkout/venv 前必须重新校验 tree 与隔离 Python")
    rollback = plan["rollback"]["steps"]
    if len(rollback) != 1 or rollback[0]["action"] != "stop_own_service":
        raise ExecutionBlocked("ComfyUI 计划必须有且仅有一个停止自有 user unit 的回滚步骤")
    model_steps = [step for step in steps if step["action"] == "download_model"]
    if (model_steps or reusing_model_assets) and (
        reusing_model_assets
        or all(_argument_after(step["command"], "--revision") == "master" for step in model_steps)
    ):
        manifest = _model_manifest(plan)
        assets = manifest.get("comfyui_assets", {}).get("master_integrity", {})
        files = tuple(
            manifest.get("comfyui_assets", {})
            .get("variants", {})
            .get(plan["model"]["variant"], {})
            .get("files", ())
        )
        expected = (
            "sha256sum",
            *(str(PurePosixPath(plan["target"]["model_root"]) / path) for path in files),
        )
        checks = [step for step in steps if tuple(step["command"]) == expected]
        if reusing_model_assets:
            service_config = next(step for step in steps if step["action"] == "start_own_service")
        else:
            service_config = next(
                step for step in steps if step["action"] == "create_service_config"
            )
        requested_files = tuple(file for step in model_steps for file in tuple(step["command"])[8:])
        expected_model_link = (
            "test",
            "-e",
            str(PurePosixPath(plan["target"]["install_root"]) / "models"),
        )
        model_link_checks = [
            step for step in steps if tuple(step["command"]) == expected_model_link
        ]
        if (
            len(checks) != 1
            or not all(path in assets for path in files)
            or (
                not reusing_model_assets
                and (
                    requested_files != files
                    or len(set(requested_files)) != len(requested_files)
                    or not {step["step_id"] for step in model_steps}.issubset(
                        checks[0].get("depends_on", [])
                    )
                )
            )
            or (reusing_model_assets and len(model_link_checks) != 1)
            or checks[0]["step_id"] not in service_config.get("depends_on", [])
        ):
            raise ExecutionBlocked("可变 ModelScope master 下载必须在建服务前逐文件校验 SHA-256")
    bundle = plan["framework"]["runtime_artifact"].get("source_bundle")
    if isinstance(bundle, Mapping):
        by_id = {step["step_id"]: step for step in steps}
        required_bundle_steps = {
            "source-bundle-absent",
            "stage-source-bundle",
            "verify-source-bundle",
            "verify-source-tree",
        }
        if not required_bundle_steps.issubset(by_id):
            raise ExecutionBlocked("暂存源码包计划缺少排他上传、哈希或 tree 校验步骤")
        if (
            tuple(by_id["source-bundle-absent"]["command"])
            != (
                "test",
                "!",
                "-e",
                bundle["remote_path"],
            )
            or tuple(by_id["stage-source-bundle"]["command"])
            != (
                "sftp-upload",
                bundle["local_path"],
                bundle["remote_path"],
            )
            or tuple(by_id["verify-source-bundle"]["command"])
            != (
                "sha256sum",
                bundle["remote_path"],
            )
            or tuple(by_id["verify-source-tree"]["command"])
            != (
                "git",
                "-C",
                plan["framework"]["runtime_artifact"]["location"],
                "rev-parse",
                "HEAD^{tree}",
            )
        ):
            raise ExecutionBlocked("暂存源码包计划命令与固定校验契约不一致")
        if (
            "source-bundle-absent" not in by_id["stage-source-bundle"].get("depends_on", [])
            or "stage-source-bundle" not in by_id["verify-source-bundle"].get("depends_on", [])
            or "verify-source-bundle" not in by_id["clone-source"].get("depends_on", [])
            or "checkout-source" not in by_id["verify-source-tree"].get("depends_on", [])
        ):
            raise ExecutionBlocked("暂存源码包步骤依赖顺序不安全")


def validate_executable_plan(
    plan: Mapping[str, Any],
    *,
    policy_path: Path = ROOT / "config/harness-policy.yaml",
    schema_path: Path = ROOT / "schemas/deployment-plan.schema.json",
) -> None:
    try:
        validate_instance(dict(plan), schema_path.name)
    except HarnessError as exc:
        raise ExecutionBlocked(str(exc)) from exc
    policy = _policy(policy_path)
    execution = policy["remote_execution"]
    if plan.get("status") != execution["executable_plan_status"]:
        raise ExecutionBlocked("仅 READY 计划可执行")
    review = plan.get("review", {})
    if review.get("status") != "APPROVED":
        raise ExecutionBlocked("计划审核未获批准")
    if plan.get("license_gate", {}).get("status") != policy["license_gate"]["allowed_ready_status"]:
        raise ExecutionBlocked("许可门禁未通过")
    _validate_lifecycle(plan)
    _validate_compatibility_basis(plan)
    _validate_adaptation_binding(plan)
    _validate_capacity_trial_actions(plan)
    framework_recipe = _validate_framework_evidence_and_recipe(plan)
    actual_hash = canonical_plan_sha256(plan)
    if not hmac.compare_digest(review.get("plan_sha256", ""), actual_hash):
        raise ExecutionBlocked("已审核计划 SHA-256 与规范化计划不一致")
    write_actions = set(execution["plan_allowed_writes"])
    protected_actions = set(execution["protected_actions"]["actions"])
    for collection in (plan["steps"], plan["rollback"]["steps"]):
        step_ids = [step["step_id"] for step in collection]
        sequences = [step["sequence"] for step in collection]
        if len(step_ids) != len(set(step_ids)) or len(sequences) != len(set(sequences)):
            raise ExecutionBlocked("步骤 ID 和序号必须唯一")
        for step in collection:
            action, action_class = step["action"], step["action_class"]
            if action == "inspect":
                expected = "READ_ONLY"
            elif action in write_actions:
                expected = "PLAN_ALLOWED_WRITE"
            elif action in protected_actions:
                expected = "PROTECTED"
            else:
                raise ExecutionBlocked(f"未知动作已被阻断：{action}")
            if action_class != expected:
                raise ExecutionBlocked(f"{step['step_id']} 的动作分类不匹配")
            if expected == "PROTECTED":
                if not _scope_authorizes(step):
                    raise ExecutionBlocked(
                        f"受保护步骤缺少限定至该步骤和动作的批准：{step['step_id']}"
                    )
                raise ExecutionBlocked(
                    "MVP 执行器绝不自动执行受保护动作；批准必须在单独且明确受监督的流程中处理"
                )
            _validate_step_command(step, plan, execution["command_policy"], framework_recipe)
    _validate_comfyui_plan_shape(plan)


def _secret_values(environ: Mapping[str, str] | None, dotenv_path: Path | None) -> tuple[str, ...]:
    values = secret_environment(environ, dotenv_path).values()
    return tuple(value for value in values if len(value) >= 4)


def _assert_plan_has_no_secret(plan: Mapping[str, Any], secrets: Sequence[str]) -> None:
    serialized = json.dumps(plan, sort_keys=True)
    if any(secret in serialized for secret in secrets):
        raise ExecutionBlocked("计划包含密钥值")
    if re.search(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----", serialized):
        raise ExecutionBlocked("计划包含私钥内容")


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _normalized_region(value: str) -> str:
    token = re.sub(r"[^A-Z]", "", value.upper())
    aliases = {
        "USA": "US",
        "UNITEDSTATES": "US",
        "UNITEDSTATESOFAMERICA": "US",
        "UNITEDKINGDOM": "UK",
        "REPUBLICOFKOREA": "KR",
        "SOUTHKOREA": "KR",
        "EUROPEANUNION": "EU",
    }
    return aliases.get(token, token)


def _validate_license_binding(plan: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    gate = plan["license_gate"]
    if gate["deployment_region"] != request["deployment_region"]:
        raise ExecutionBlocked("许可门禁区域与请求不一致")
    if gate["intended_use"] != request["intended_use"]:
        raise ExecutionBlocked("许可门禁预期用途与请求不一致")
    manifest_path = _resolved_repo_file(plan["model"]["recipe_ref"], ROOT / "models")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model", {}).get("id") != plan["model"]["id"]:
        raise ExecutionBlocked("模型清单 ID 与计划不一致")
    license_data = manifest.get("license", {})
    if gate["license_type"] != license_data.get("type") or gate["source_url"] != license_data.get(
        "source"
    ):
        raise ExecutionBlocked("许可门禁类型/来源与模型清单不一致")
    excluded = {
        _normalized_region(value)
        for value in license_data.get("excluded_territories_without_separate_license", [])
    }
    if _normalized_region(request["deployment_region"]) in excluded and not gate.get(
        "authorization_reference"
    ):
        raise ExecutionBlocked("部署区域需要单独许可授权")


def _comfyui_runtime_gate(
    gate: GateResult, _host_profile: Mapping[str, Any], plan: Mapping[str, Any]
) -> GateResult:
    """Require reviewed runtime paths; remote checks run before any write operation."""
    if plan["framework"]["name"] != "comfyui":
        return gate
    blockers = list(gate.blockers)
    try:
        _comfy_bootstrap(plan)
    except ExecutionBlocked as exc:
        blockers.append(str(exc))
    if not blockers:
        return gate
    return GateResult(
        "BLOCKED",
        tuple(blockers),
        warnings=gate.warnings,
        recommendations=gate.recommendations
        + ("请记录经只读探测确认的隔离 uv/Python 绝对路径，再重新审核计划。",),
    )


def authorize_execution(
    plan: Mapping[str, Any],
    request: Mapping[str, Any],
    host_profile: Mapping[str, Any],
    *,
    required_cuda: str | None = None,
) -> GateResult:
    """将已审核计划绑定到已校验请求和精确主机观测。"""
    try:
        validate_instance(dict(request), "deployment-request.schema.json")
        validate_instance(dict(host_profile), "host-profile.schema.json")
    except HarnessError as exc:
        raise ExecutionBlocked(str(exc)) from exc
    mismatches: list[str] = []
    if plan.get("request_id") != request.get("request_id"):
        mismatches.append("request_id")
    if plan.get("host_profile_observed_at") != host_profile.get("observed_at"):
        mismatches.append("host_profile_observed_at")
    plan_target = plan.get("target", {})
    request_target = request.get("target", {})
    for field in ("gpu_ids", "install_root", "model_root"):
        if plan_target.get(field) != request_target.get(field):
            mismatches.append(f"target.{field}")
    if plan_target.get("host_id") != host_profile.get("host_id"):
        mismatches.append("target.host_id")
    if plan.get("model", {}).get("id") != request.get("model", {}).get("id"):
        mismatches.append("model.id")
    if plan.get("model", {}).get("variant") != request.get("model", {}).get("variant"):
        mismatches.append("model.variant")
    if plan.get("service") != request.get("service"):
        mismatches.append("service")
    preference = request.get("framework_preference")
    if plan.get("framework", {}).get("name") != preference:
        mismatches.append("framework_preference")
    if mismatches:
        raise ExecutionBlocked("计划/请求/主机制品不匹配：" + "、".join(mismatches))
    _validate_license_binding(plan, request)
    _validate_catalog_request_limits(plan, request)
    _validate_catalog_profile_host(plan, host_profile)
    reviewed_cuda = plan["compatibility"]["required_cuda"]
    if required_cuda is not None and required_cuda != reviewed_cuda:
        raise ExecutionBlocked("CLI CUDA 要求与已审核计划不一致")
    environment = plan.get("environment", {})
    gate = host_preflight(
        request,
        host_profile,
        required_cuda=reviewed_cuda,
        environment_strategy=environment.get("strategy"),
        environment_isolated=bool(environment.get("isolated")),
    )
    gate = _comfyui_runtime_gate(gate, host_profile, plan)
    if not gate.passed:
        raise ExecutionBlocked("预检未通过：" + "；".join(gate.blockers))
    return gate


@contextmanager
def single_writer_lock(host_id: str, lock_directory: Path = Path("/tmp")) -> Iterator[None]:
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host_id)
    path = lock_directory / f"model-deployment-harness-{safe_host}.lock"
    lock_directory.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionBlocked(f"另一执行器持有 {host_id} 的写入锁") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def remote_writer_lock(
    transport: CommandTransport,
    acquire_command: Sequence[str],
    release_command: Sequence[str],
) -> Iterator[None]:
    try:
        acquired = transport.run(tuple(acquire_command), timeout=20)
    except Exception as exc:
        raise ExecutionBlocked(f"远端写入锁获取失败：{exc.__class__.__name__}") from exc
    if acquired.returncode != 0:
        raise ExecutionBlocked("远端写入锁已被持有或不可用；绝不自动移除陈旧锁")
    try:
        yield
    finally:
        try:
            released = transport.run(tuple(release_command), timeout=20)
        except Exception as exc:
            raise ExecutionBlocked(f"远端写入锁释放失败：{exc.__class__.__name__}") from exc
        if released.returncode != 0:
            raise ExecutionBlocked("释放远端写入锁失败；需要人工检查")


def _live_preflight(
    transport: CommandTransport,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    reviewed_profile: Mapping[str, Any],
    required_cuda: str | None,
    probe_collector: Callable[[CommandTransport], Mapping[str, Any]] | None,
) -> None:
    if probe_collector is None:
        live_profile = collect_host_profile(
            transport,
            host_id=plan["target"]["host_id"],
            aliases=(),
        )
    else:
        live_profile = probe_collector(transport)
    try:
        validate_instance(dict(live_profile), "host-profile.schema.json")
    except HarnessError as exc:
        raise ExecutionBlocked(f"实时主机探测无效：{exc}") from exc
    if live_profile.get("host_id") != plan["target"]["host_id"]:
        raise ExecutionBlocked("实时主机身份偏离已审核目标")
    reviewed_identity = reviewed_profile.get("identity", {})
    live_identity = live_profile.get("identity", {})
    reviewed_values = {
        str(reviewed_identity.get("hostname", "")),
        *(str(value) for value in reviewed_identity.get("addresses", [])),
    }
    live_values = {
        str(live_identity.get("hostname", "")),
        *(str(value) for value in live_identity.get("addresses", [])),
    }
    if not reviewed_values.intersection(live_values):
        raise ExecutionBlocked("实时 SSH 主机身份与已审核发现不一致")
    locator = request["target"]["host"]
    requested_values = {str(locator[key]) for key in ("address", "hostname") if locator.get(key)}
    if requested_values and not requested_values.intersection(live_values):
        raise ExecutionBlocked("实时 SSH 主机与用户提供的选择器不一致")
    environment = plan["environment"]
    if plan["compatibility"]["basis"] == "CATALOG_PROFILE":
        _validate_catalog_limits(plan)
    _validate_catalog_profile_host(plan, live_profile)
    gate = host_preflight(
        request,
        live_profile,
        required_cuda=plan["compatibility"]["required_cuda"],
        environment_strategy=environment["strategy"],
        environment_isolated=environment["isolated"],
    )
    gate = _comfyui_runtime_gate(gate, live_profile, plan)
    if not gate.passed:
        raise ExecutionBlocked("实时预检未通过：" + "；".join(gate.blockers))


def _probe_runtime_artifact(plan: Mapping[str, Any], transport: CommandTransport) -> None:
    runtime = plan["framework"]["runtime_artifact"]
    try:
        result = transport.run(tuple(runtime["probe_command"]), timeout=20)
    except Exception as exc:
        raise ExecutionBlocked(f"框架运行时探测失败：{exc.__class__.__name__}") from exc
    if result.returncode != 0 or result.stdout.strip() != runtime["revision"]:
        raise ExecutionBlocked("已安装框架运行时与已审核不可变 pin 不一致")


def _stage_source_bundle(
    plan: Mapping[str, Any], transport: CommandTransport, *, timeout: int
) -> CommandResult:
    bundle = plan["framework"]["runtime_artifact"].get("source_bundle")
    if not isinstance(bundle, Mapping):  # defensive: validated before execution
        raise ExecutionBlocked("暂存源码包步骤未绑定来源制品")
    upload_new = getattr(transport, "upload_new", None)
    if not callable(upload_new):
        raise ExecutionBlocked("SSH 传输不支持排他暂存源码包上传")
    try:
        upload_new(Path(bundle["local_path"]), bundle["remote_path"], timeout=timeout)
    except Exception as exc:
        raise ExecutionBlocked(f"暂存源码包上传失败：{exc.__class__.__name__}") from exc
    return CommandResult(
        ("sftp-upload", bundle["local_path"], bundle["remote_path"]),
        0,
        "source bundle uploaded exclusively\n",
        "",
    )


def _validate_source_bundle_observation(
    plan: Mapping[str, Any], step: Mapping[str, Any], result: CommandResult
) -> None:
    bundle = plan["framework"]["runtime_artifact"].get("source_bundle")
    if not isinstance(bundle, Mapping):
        return
    if step["step_id"] == "verify-source-bundle":
        observed = result.stdout.strip().split(maxsplit=1)
        if not observed or not hmac.compare_digest(observed[0].lower(), bundle["sha256"].lower()):
            raise ExecutionBlocked("远端暂存源码包 SHA-256 不匹配")


def _validate_source_tree_observation(
    plan: Mapping[str, Any],
    framework_recipe: Mapping[str, Any],
    step: Mapping[str, Any],
    result: CommandResult,
) -> None:
    if step["step_id"] != "verify-source-tree":
        return
    source_tree = framework_recipe.get("framework", {}).get("source_tree")
    if not isinstance(source_tree, str) or not hmac.compare_digest(
        result.stdout.strip().lower(), source_tree.lower()
    ):
        raise ExecutionBlocked("远端 checkout tree 与官方固定 tree 不匹配")


def _validate_model_asset_observation(
    plan: Mapping[str, Any], step: Mapping[str, Any], result: CommandResult
) -> None:
    if step["step_id"] != "verify-model-assets":
        return
    manifest = _model_manifest(plan)
    assets = manifest.get("comfyui_assets", {}).get("master_integrity", {})
    expected = {
        str(PurePosixPath(plan["target"]["model_root"]) / path): details["sha256"].lower()
        for path, details in assets.items()
    }
    observed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            observed[parts[1].strip()] = parts[0].lower()
    files = tuple(
        manifest.get("comfyui_assets", {})
        .get("variants", {})
        .get(plan["model"]["variant"], {})
        .get("files", ())
    )
    if any(
        observed.get(str(PurePosixPath(plan["target"]["model_root"]) / path))
        != expected.get(str(PurePosixPath(plan["target"]["model_root"]) / path))
        for path in files
    ):
        raise ExecutionBlocked("ModelScope 下载资产 SHA-256 与官方 API manifest 不匹配")


def _plan_bootstraps_runtime(plan: Mapping[str, Any]) -> bool:
    return any(
        step["action"] in {"clone_source_checkout", "checkout_source_revision"}
        for step in plan["steps"]
    )


def _execute_plan_unrecorded(
    plan: Mapping[str, Any],
    transport: CommandTransport,
    *,
    request: Mapping[str, Any],
    host_profile: Mapping[str, Any],
    required_cuda: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    lock_directory: Path = Path("/tmp"),
    timeout: int = 300,
    _probe_collector: Callable[[CommandTransport], Mapping[str, Any]] | None = None,
) -> ExecutionResult:
    """按顺序执行精确计划 argv，任一失败或漂移即停止。"""
    validate_executable_plan(plan)
    framework_recipe = _validate_framework_evidence_and_recipe(plan)
    authorize_execution(plan, request, host_profile, required_cuda=required_cuda)
    process_env = dict(environ if environ is not None else os.environ)
    file_env = secret_environment({}, dotenv_path)
    secrets = _secret_values(process_env, dotenv_path)
    _assert_plan_has_no_secret(plan, secrets)
    completed: set[str] = set()
    results: list[StepResult] = []
    _live_preflight(
        transport,
        request,
        plan,
        host_profile,
        required_cuda,
        _probe_collector,
    )
    bootstraps_runtime = _plan_bootstraps_runtime(plan)
    if not bootstraps_runtime:
        _probe_runtime_artifact(plan, transport)
    lock = plan["executor_controls"]["remote_writer_lock"]
    with (
        single_writer_lock(plan["target"]["host_id"], lock_directory),
        remote_writer_lock(transport, lock["acquire_command"], lock["release_command"]),
    ):
        if not bootstraps_runtime:
            _probe_runtime_artifact(plan, transport)
        for step in sorted(plan["steps"], key=lambda item: item["sequence"]):
            dependencies = set(step.get("depends_on", []))
            if not dependencies.issubset(completed):
                raise ExecutionBlocked(f"{step['step_id']} 的前置条件失败：依赖尚未完成")
            if step["action_class"] != "READ_ONLY":
                _live_preflight(
                    transport,
                    request,
                    plan,
                    host_profile,
                    required_cuda,
                    _probe_collector,
                )
            names = step.get("environment_variable_names", [])
            step_env = {}
            for name in names:
                value = process_env.get(name, file_env.get(name))
                if value is None:
                    raise ExecutionBlocked(f"缺少必需环境变量：{name}")
                step_env[name] = value
            if step["action"] == "start_own_service":
                step_env["CUDA_VISIBLE_DEVICES"] = ",".join(
                    str(gpu_id) for gpu_id in plan["target"]["gpu_ids"]
                )
            argv = tuple(step["command"])
            step_started_at = datetime.now(timezone.utc).isoformat()
            try:
                result: CommandResult = (
                    _stage_source_bundle(plan, transport, timeout=timeout)
                    if step["action"] == "stage_source_bundle"
                    else transport.run(
                        argv, timeout=timeout, cwd=step.get("working_directory"), env=step_env
                    )
                )
            except ExecutionBlocked:
                raise
            except Exception as exc:
                raise ExecutionBlocked(
                    f"{step['step_id']} 的 SSH 执行失败：{exc.__class__.__name__}"
                ) from exc
            if tuple(result.argv) != argv:
                raise ExecutionBlocked(f"检测到 {step['step_id']} 的命令漂移")
            recorded = StepResult(
                step["step_id"],
                step_started_at,
                datetime.now(timezone.utc).isoformat(),
                result.returncode,
                _redact(result.stdout, secrets),
                _redact(result.stderr, secrets),
            )
            results.append(recorded)
            if result.returncode != 0:
                return ExecutionResult(
                    "BLOCKED",
                    tuple(results),
                    f"步骤 {step['step_id']} 以退出状态 {result.returncode} 失败",
                )
            _validate_source_bundle_observation(plan, step, result)
            _validate_source_tree_observation(plan, framework_recipe, step, result)
            _validate_model_asset_observation(plan, step, result)
            completed.add(step["step_id"])
            if step["action"] == "checkout_source_revision":
                _probe_runtime_artifact(plan, transport)
    return ExecutionResult("EXECUTED", tuple(results))


def execute_plan(
    plan: Mapping[str, Any],
    transport: CommandTransport,
    *,
    request: Mapping[str, Any],
    host_profile: Mapping[str, Any],
    required_cuda: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    lock_directory: Path = Path("/tmp"),
    timeout: int = 300,
    _probe_collector: Callable[[CommandTransport], Mapping[str, Any]] | None = None,
    archive: DeploymentArchive | None = None,
) -> ExecutionResult:
    """执行计划；提供档案时自动保存每步脱敏结果及失败事故。"""
    started_at = datetime.now(timezone.utc).isoformat()
    deployment_context = _deployment_archive_context(plan, request)
    try:
        result = _execute_plan_unrecorded(
            plan,
            transport,
            request=request,
            host_profile=host_profile,
            required_cuda=required_cuda,
            environ=environ,
            dotenv_path=dotenv_path,
            lock_directory=lock_directory,
            timeout=timeout,
            _probe_collector=_probe_collector,
        )
    except Exception as exc:
        if archive is not None:
            blocker = _redact(
                str(exc),
                _secret_values(dict(environ if environ is not None else os.environ), dotenv_path),
            )
            archive.record(
                stage="EXECUTE",
                status="BLOCKED",
                summary="远程执行在完成前被阻止",
                host_id=str(plan.get("target", {}).get("host_id", "unknown")),
                details={
                    "plan_sha256": plan.get("review", {}).get("plan_sha256"),
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "steps": [],
                    "blocker": blocker,
                    "deployment": deployment_context,
                },
            )
        raise
    if archive is not None:
        archive.record(
            stage="EXECUTE",
            status=result.status,
            summary=(
                "已执行完整审核计划" if result.status == "EXECUTED" else "远程执行因步骤失败而停止"
            ),
            host_id=str(plan["target"]["host_id"]),
            details={
                "plan_sha256": plan["review"]["plan_sha256"],
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "started_at": step.started_at,
                        "completed_at": step.completed_at,
                        "returncode": step.returncode,
                        "stdout": step.stdout,
                        "stderr": step.stderr,
                    }
                    for step in result.steps
                ],
                "blocker": result.blocker,
                "environment": (
                    f"host_id={plan['target']['host_id']}; "
                    f"framework={plan['framework']['name']}@{plan['framework']['version']}"
                ),
                "deployment": deployment_context,
            },
        )
    return result


def _deployment_archive_context(
    plan: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "request_ref": f"request:{request['request_id']}",
        "plan_ref": f"sha256:{plan['review']['plan_sha256']}",
        "model": {
            "id": plan["model"]["id"],
            "variant": plan["model"]["variant"],
            "path": plan["target"]["model_root"],
        },
        "framework": {
            "name": plan["framework"]["name"],
            "version": plan["framework"]["version"],
        },
        "target": {
            "gpu_ids": list(plan["target"]["gpu_ids"]),
            "install_root": plan["target"]["install_root"],
            "bind_host": plan["service"]["bind_host"],
            "port": plan["service"]["port"],
        },
    }


def archive_reviewed_lifecycle(
    archive: DeploymentArchive,
    plan: Mapping[str, Any],
    *,
    request_path: Path,
    host_profile_path: Path,
    plan_path: Path,
) -> None:
    """校验 READY 计划后，把规划前六阶段及其原始制品逐项归档。"""
    validate_executable_plan(plan)
    if load_document(plan_path) != plan:
        raise ExecutionBlocked("待归档计划文件与已校验计划不一致")
    extras = {
        "REQUIREMENT_GATE": (request_path,),
        "HOST_DISCOVERY": (host_profile_path,),
        "PLAN_REVIEW": (plan_path,),
    }
    for transition in plan["lifecycle"]["transitions"]:
        stage = str(transition["stage"])
        if stage not in {
            "INTAKE",
            "REQUIREMENT_GATE",
            "HOST_DISCOVERY",
            "RESEARCH",
            "PLAN",
            "PLAN_REVIEW",
        }:
            continue
        lifecycle_path = _resolved_repo_file(str(transition["artifact"]["path"]), ROOT)
        artifacts = list(dict.fromkeys((lifecycle_path, *extras.get(stage, ()))))
        archive.record(
            stage=stage,
            status="PASS",
            summary=f"{stage} 阶段已通过并保存校验证据",
            host_id=str(plan["target"]["host_id"]),
            artifacts=artifacts,
            occurred_at=str(transition["completed_at"]),
            details={
                "plan_sha256": plan["review"]["plan_sha256"],
                "source_sha256": transition["artifact"]["sha256"],
            },
        )


def _host_matches_artifacts(
    host: str, request: Mapping[str, Any], profile: Mapping[str, Any]
) -> bool:
    locator = request.get("target", {}).get("host", {})
    requested_network_values = {
        str(locator[key]) for key in ("address", "hostname", "alias") if locator.get(key)
    }
    identity = profile.get("identity", {})
    observed_values = {
        str(identity.get("hostname", "")),
        *(str(value) for value in identity.get("addresses", [])),
        *(str(value) for value in identity.get("aliases", [])),
    }
    if requested_network_values:
        return host in requested_network_values and host in observed_values
    return locator.get("host_id") == profile.get("host_id") and host in observed_values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在一台 SSH 主机上执行一份已审核的 READY 计划")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--host-profile", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--required-cuda")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--lock-directory", type=Path, default=Path("/tmp"))
    parser.add_argument("--timeout", type=int, default=7200)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    transport: CommandTransport | None = None
    archive: DeploymentArchive | None = None
    execution_started = False
    try:
        plan = load_document(args.plan)
        request = load_document(args.request)
        profile = load_document(args.host_profile)
        archive = DeploymentArchive(plan["deployment_id"])
        archive_reviewed_lifecycle(
            archive,
            plan,
            request_path=args.request,
            host_profile_path=args.host_profile,
            plan_path=args.plan,
        )
        if not _host_matches_artifacts(args.host, request, profile):
            raise ExecutionBlocked("SSH 主机未同时绑定至请求和观测身份")
        locator = request["target"]["host"]
        if args.username != locator["ssh_username"] or args.port != locator["ssh_port"]:
            raise ExecutionBlocked("SSH 用户名/端口与明确请求意图不一致")
        transport = ParamikoTransport.connect(
            args.host,
            username=args.username,
            port=args.port,
            dotenv_path=args.env_file,
        )
        execution_started = True
        result = execute_plan(
            plan,
            transport,
            request=request,
            host_profile=profile,
            required_cuda=args.required_cuda,
            dotenv_path=args.env_file,
            lock_directory=args.lock_directory,
            timeout=args.timeout,
            archive=archive,
        )
        summary = {
            "status": result.status,
            "steps": [
                {"step_id": step.step_id, "returncode": step.returncode} for step in result.steps
            ],
            "blocker": result.blocker,
        }
        print(json.dumps(summary, indent=2))
        return 0 if result.status == "EXECUTED" else 3
    except (ExecutionBlocked, HarnessError, OSError, ValueError) as exc:
        blocker = _redact(str(exc), _secret_values(dict(os.environ), args.env_file))
        if archive is not None and not execution_started:
            with suppress(HarnessError, OSError):
                archive.record(
                    stage="EXECUTE",
                    status="BLOCKED",
                    summary="远程执行尚未开始便被安全门禁阻止",
                    host_id=str(plan.get("target", {}).get("host_id", "unknown")),
                    details={
                        "plan_sha256": plan.get("review", {}).get("plan_sha256"),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "steps": [],
                        "blocker": blocker,
                        "deployment": _deployment_archive_context(plan, request),
                    },
                )
        print(json.dumps({"status": "BLOCKED", "blocker": blocker}, indent=2))
        return 2
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
