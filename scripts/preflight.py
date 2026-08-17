"""需求与已观测主机门禁。本模块绝不修改主机。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts._common import HarnessError, load_document, validate_instance
from scripts.intake import discovery_missing_fields

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GateResult:
    status: str
    blockers: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()

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
            "请选择 NVIDIA 驱动满足已审核运行时要求的主机；"
            "容器或 venv 隔离无法修复不兼容的主机驱动"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "discovery":
            result = discovery_gate(load_document(args.draft or args.request))
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
        return 0 if result.passed else 3
    except (HarnessError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "blockers": [str(exc)]}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
