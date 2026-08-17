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
from scripts.preflight import GateResult, host_preflight
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
        re.fullmatch(r"[0-9a-fA-F]{7,40}", value)
        or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value)
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
            raise ExecutionBlocked(
                f"生命周期制品缺失或位于仓库外：{reference['path']}"
            )
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


def _validate_framework_evidence_and_recipe(plan: Mapping[str, Any]) -> None:
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
    if executable_name is None:
        raise ExecutionBlocked("框架没有已实现的可执行文件绑定")
    expected_executable = str(PurePosixPath(runtime["location"]) / ".venv/bin" / executable_name)
    if runtime["executable"] != expected_executable:
        raise ExecutionBlocked("框架可执行文件未绑定到已审核 checkout")
    evidence = {item["evidence_id"]: item for item in plan["evidence"]}
    for evidence_id in framework["evidence_ids"]:
        item = evidence.get(evidence_id)
        if item is None:
            raise ExecutionBlocked(f"框架证据引用悬挂：{evidence_id}")
        if item["source"]["authority_tier"] not in {"S", "A"}:
            raise ExecutionBlocked("关键框架决策缺少 S/A 级证据")
        if item["confidence"] != "HIGH" or item["inference"]:
            raise ExecutionBlocked("关键框架证据必须具有高置信度且为直接证据")


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
    step: Mapping[str, Any], plan: Mapping[str, Any], command_policy: Mapping[str, Any]
) -> None:
    argv = tuple(step["command"])
    if not argv or any("\x00" in value for value in argv):
        raise ExecutionBlocked(f"{step['step_id']} 的 argv 无效")
    executable = PurePosixPath(argv[0]).name
    if executable in set(command_policy["shell_interpreters_forbidden"]):
        raise ExecutionBlocked(f"{step['step_id']} 禁止使用 shell 解释器")
    allowed = set(command_policy.get(step["action"], []))
    if executable not in allowed:
        raise ExecutionBlocked(
            f"命令可执行文件与声明动作 {step['action']} 不一致：{executable}"
        )
    roots = (plan["target"]["install_root"], plan["target"]["model_root"])
    action = step["action"]
    if action == "inspect":
        if len(argv) != 3 or argv[1] not in {"-d", "-e", "-f"} or not _under_remote_root(
            argv[2], roots
        ):
            raise ExecutionBlocked("计划 inspect 步骤仅限对目标根目录执行 test")
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
        if not valid_shape or not _under_remote_root(
            destination, (plan["target"]["install_root"],)
        ):
            raise ExecutionBlocked("隔离 venv 的命令/路径与声明动作不一致")
    elif action == "pull_container":
        if (
            len(argv) != 3
            or argv[1] != "pull"
            or not re.search(r"@sha256:[0-9a-fA-F]{64}$", argv[2])
        ):
            raise ExecutionBlocked("拉取容器必须使用不可变镜像摘要")
    elif action == "download_model":
        revision = _argument_after(argv, "--revision")
        destination = _argument_after(argv, "--local-dir")
        if argv[:2] != ("hf", "download") or not revision or not re.fullmatch(
            r"[0-9a-fA-F]{40}", revision
        ):
            raise ExecutionBlocked("下载模型需要带 40 位十六进制修订版本的 hf download")
        if destination is None or not _under_remote_root(
            destination, (plan["target"]["model_root"],)
        ):
            raise ExecutionBlocked("模型下载目标越出 model_root")
    elif action == "create_service_config":
        non_options = [value for value in argv[1:] if not value.startswith("-")]
        if len(non_options) < 2 or not all(
            _under_remote_root(value, roots) for value in non_options[-2:]
        ):
            raise ExecutionBlocked(
                "服务配置源/目标必须位于目标根目录下"
            )
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
                raise ExecutionBlocked(
                    "Docker 服务启动必须符合已审核的精确 run 语法"
                )
        elif executable == "podman":
            raise ExecutionBlocked("MVP 执行器尚未实现 Podman 服务启动")
        elif executable == "sglang":
            expected_argv = (
                plan["framework"]["runtime_artifact"]["executable"], "serve",
                "--model-path", plan["target"]["model_root"],
                "--model-variant", plan["model"]["variant"],
                "--num-gpus", str(len(plan["target"]["gpu_ids"])),
                "--host", plan["service"]["bind_host"],
                "--port", str(plan["service"]["port"]),
            )
            if argv != expected_argv:
                raise ExecutionBlocked("SGLang 服务命令与已审核启动命令不一致")
        elif executable == "vllm":
            expected_argv = (
                plan["framework"]["runtime_artifact"]["executable"], "serve",
                plan["target"]["model_root"], "--omni", "--trust-remote-code",
                "--num-gpus", str(len(plan["target"]["gpu_ids"])),
                "--host", plan["service"]["bind_host"],
                "--port", str(plan["service"]["port"]),
            )
            if argv != expected_argv:
                raise ExecutionBlocked("vLLM 服务命令与已审核启动命令不一致")


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
    _validate_framework_evidence_and_recipe(plan)
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
                        "受保护步骤缺少限定至该步骤和动作的批准："
                        f"{step['step_id']}"
                    )
                raise ExecutionBlocked(
                    "MVP 执行器绝不自动执行受保护动作；批准必须在单独且明确受监督的流程中处理"
                )
            _validate_step_command(step, plan, execution["command_policy"])


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
        for value in license_data.get(
            "excluded_territories_without_separate_license", []
        )
    }
    if _normalized_region(request["deployment_region"]) in excluded and not gate.get(
        "authorization_reference"
    ):
        raise ExecutionBlocked("部署区域需要单独许可授权")


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
        raise ExecutionBlocked(
            f"远端写入锁获取失败：{exc.__class__.__name__}"
        ) from exc
    if acquired.returncode != 0:
        raise ExecutionBlocked(
            "远端写入锁已被持有或不可用；绝不自动移除陈旧锁"
        )
    try:
        yield
    finally:
        try:
            released = transport.run(tuple(release_command), timeout=20)
        except Exception as exc:
            raise ExecutionBlocked(
                f"远端写入锁释放失败：{exc.__class__.__name__}"
            ) from exc
        if released.returncode != 0:
            raise ExecutionBlocked(
                "释放远端写入锁失败；需要人工检查"
            )


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
    gate = host_preflight(
        request,
        live_profile,
        required_cuda=plan["compatibility"]["required_cuda"],
        environment_strategy=environment["strategy"],
        environment_isolated=environment["isolated"],
    )
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
    _probe_runtime_artifact(plan, transport)
    lock = plan["executor_controls"]["remote_writer_lock"]
    with (
        single_writer_lock(plan["target"]["host_id"], lock_directory),
        remote_writer_lock(transport, lock["acquire_command"], lock["release_command"]),
    ):
            _probe_runtime_artifact(plan, transport)
            for step in sorted(plan["steps"], key=lambda item: item["sequence"]):
                dependencies = set(step.get("depends_on", []))
                if not dependencies.issubset(completed):
                    raise ExecutionBlocked(
                        f"{step['step_id']} 的前置条件失败：依赖尚未完成"
                    )
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
                    result: CommandResult = transport.run(
                        argv, timeout=timeout, cwd=step.get("working_directory"), env=step_env
                    )
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
                completed.add(step["step_id"])
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
                _secret_values(
                    dict(environ if environ is not None else os.environ), dotenv_path
                ),
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
                "已执行完整审核计划"
                if result.status == "EXECUTED"
                else "远程执行因步骤失败而停止"
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
        lifecycle_path = _resolved_repo_file(
            str(transition["artifact"]["path"]), ROOT
        )
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
