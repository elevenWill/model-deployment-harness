"""跨多轮对话保存需求草稿，并把内部字段翻译成白话状态。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from scripts._common import (
        ROOT,
        HarnessError,
        assert_no_secrets,
        atomic_write_json,
        load_document,
        validate_instance,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from _common import (  # type: ignore[no-redef]
        ROOT,
        HarnessError,
        assert_no_secrets,
        atomic_write_json,
        load_document,
        validate_instance,
    )


HOST_SELECTORS = ("host_id", "hostname", "address", "alias")
FIELD_LABELS = {
    "requested_by": "由谁发起这次部署（姓名或操作员标识）",
    "target.host.selector": "服务器地址、主机名、别名或已登记的主机 ID",
    "target.host.ssh_username": "SSH 登录用户名",
    "target.host.ssh_port": "SSH 端口",
    "target.gpu_ids": "要使用的 GPU 编号",
    "target.install_root": "程序安装目录",
    "target.model_root": "模型文件目录",
    "model.id": "要安装的模型",
    "model.variant": "模型版本或变体",
    "framework_preference": "主机检查后确定的推理框架",
    "service.mode": "服务如何运行（先前台验证、长期后台服务或容器）",
    "service.bind_host": "服务监听地址",
    "service.port": "服务端口",
    "existing_environment_policy": "是否必须保留现有环境并使用隔离环境",
    "intended_use": "模型用途",
    "deployment_region": "部署地区",
}


@dataclass(frozen=True)
class DraftReadiness:
    discovery_status: str
    discovery_missing: tuple[str, ...]
    request_status: str
    request_missing: tuple[str, ...]
    pending_resolution: tuple[str, ...]
    request_blockers: tuple[str, ...]
    execution_status: str
    execution_blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "host_discovery": {
                "status": self.discovery_status,
                "missing_fields": list(self.discovery_missing),
            },
            "deployment_request": {
                "status": self.request_status,
                "missing_fields": list(self.request_missing),
                "pending_resolution": list(self.pending_resolution),
                "blockers": list(self.request_blockers),
            },
            "execution": {
                "status": self.execution_status,
                "blockers": list(self.execution_blockers),
            },
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _deep_merge(existing: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(existing))
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = deepcopy(value)
    return merged


def _intent_update(update: Mapping[str, Any]) -> Mapping[str, Any]:
    if "intent" not in update:
        return update
    if set(update) != {"intent"} or not isinstance(update["intent"], Mapping):
        raise HarnessError("更新文件包含 intent 时，顶层只能有 intent 对象")
    return update["intent"]


def create_draft(
    draft_id: str,
    update: Mapping[str, Any] | None = None,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    patch = dict(_intent_update(update or {}))
    assert_no_secrets(patch)
    draft = {
        "schema_version": "1.0",
        "draft_id": draft_id,
        "updated_at": updated_at or _now(),
        "intent": patch,
    }
    validate_instance(draft, "intake-draft.schema.json")
    return draft


def merge_draft(
    draft: Mapping[str, Any],
    update: Mapping[str, Any],
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    current = deepcopy(dict(draft))
    validate_instance(current, "intake-draft.schema.json")
    patch = dict(_intent_update(update))
    assert_no_secrets(patch)
    current["intent"] = _deep_merge(current["intent"], patch)
    current["updated_at"] = updated_at or _now()
    validate_instance(current, "intake-draft.schema.json")
    return current


def _at(document: Mapping[str, Any], dotted: str) -> object | None:
    current: object = document
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _has_selector(intent: Mapping[str, Any]) -> bool:
    host = _at(intent, "target.host")
    return isinstance(host, Mapping) and any(
        isinstance(host.get(key), str) and bool(str(host[key]).strip())
        for key in HOST_SELECTORS
    )


def discovery_missing_fields(source: Mapping[str, Any]) -> tuple[str, ...]:
    """只检查建立严格 SSH 只读连接所需的用户意图。"""
    intent = source.get("intent", source)
    if not isinstance(intent, Mapping):
        return ("target.host.selector", "target.host.ssh_username", "target.host.ssh_port")
    missing: list[str] = []
    if not _has_selector(intent):
        missing.append("target.host.selector")
    username = _at(intent, "target.host.ssh_username")
    if not isinstance(username, str) or not username.strip():
        missing.append("target.host.ssh_username")
    port = _at(intent, "target.host.ssh_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        missing.append("target.host.ssh_port")
    return tuple(missing)


def _effective_intent(intent: Mapping[str, Any]) -> dict[str, Any]:
    effective = deepcopy(dict(intent))
    preferences = effective.pop("preferences", {})
    effective.pop("license_acceptance", None)
    if isinstance(preferences, Mapping):
        if (
            "existing_environment_policy" not in effective
            and preferences.get("environment_isolation")
            in {"isolated_uv", "isolated_venv", "container"}
        ):
            effective["existing_environment_policy"] = "PRESERVE_AND_ISOLATE"
        port_policy = preferences.get("port_policy")
        if (
            isinstance(port_policy, Mapping)
            and port_policy.get("strategy") == "exact"
            and isinstance(port_policy.get("preferred_port"), int)
        ):
            service = effective.setdefault("service", {})
            if isinstance(service, dict) and "port" not in service:
                service["port"] = port_policy["preferred_port"]
    return effective


def materialize_request(draft: Mapping[str, Any]) -> dict[str, Any]:
    """把已解析为精确值的草稿变成最终请求，并通过完整 Schema 校验。"""
    validate_instance(dict(draft), "intake-draft.schema.json")
    request = _effective_intent(draft["intent"])
    request.setdefault("schema_version", "1.0")
    request.setdefault("request_id", draft["draft_id"])
    request.setdefault("requested_at", draft["updated_at"])
    validate_instance(request, "deployment-request.schema.json")
    return request


def evaluate_draft(
    draft: Mapping[str, Any],
    *,
    policy_path: Path = ROOT / "config/harness-policy.yaml",
) -> DraftReadiness:
    validate_instance(dict(draft), "intake-draft.schema.json")
    intent = draft["intent"]
    effective = _effective_intent(intent)
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    required = policy["requirement_gate"]["required_user_intent"]
    missing: list[str] = []
    pending: list[str] = []
    for path in required:
        lookup = "target.host.selector" if path == "target.host" else path
        value = _has_selector(effective) if path == "target.host" else _at(effective, path)
        if value is not None and value is not False:
            continue
        port_policy = _at(intent, "preferences.port_policy.strategy")
        framework_policy = _at(intent, "preferences.framework_selection")
        if (
            path == "framework_preference"
            and framework_policy == "delegate_to_supported_recipe"
        ) or (path == "service.port" and port_policy == "prefer_default_then_available"):
            pending.append(path)
        else:
            missing.append(lookup)

    discovery_missing = discovery_missing_fields(draft)
    request_blockers: list[str] = []
    if missing:
        request_status = "NEEDS_USER_INPUT"
    elif pending:
        request_status = "PENDING_RESOLUTION"
    else:
        try:
            materialize_request(draft)
        except HarnessError as exc:
            request_status = "BLOCKED"
            request_blockers.append(str(exc))
        else:
            request_status = "PASS"

    execution_blockers: list[str] = []
    if request_status != "PASS":
        execution_blockers.append("完整部署请求尚未落成精确值")
    license_status = _at(intent, "license_acceptance.status")
    if license_status != "ACCEPTED":
        execution_blockers.append("模型许可尚未在计划中确认通过")
    if _at(intent, "preferences.download_source") == "modelscope":
        execution_blockers.append("ModelScope 下载尚待写入部署计划并审核执行适配")
    execution_blockers.append("尚无已审核且状态为 READY 的精确部署计划")

    return DraftReadiness(
        "PASS" if not discovery_missing else "NEEDS_USER_INPUT",
        discovery_missing,
        request_status,
        tuple(missing),
        tuple(pending),
        tuple(request_blockers),
        "BLOCKED",
        tuple(execution_blockers),
    )


def _confirmed_items(intent: Mapping[str, Any]) -> list[str]:
    items: list[str] = []
    host = _at(intent, "target.host")
    if isinstance(host, Mapping):
        selector = next((host.get(key) for key in HOST_SELECTORS if host.get(key)), None)
        if selector:
            items.append(f"服务器 {selector}")
        if host.get("ssh_username"):
            items.append(f"SSH 用户名 {host['ssh_username']}")
        if host.get("ssh_port"):
            items.append(f"SSH 端口 {host['ssh_port']}")
    gpu_ids = _at(intent, "target.gpu_ids")
    if gpu_ids:
        items.append("GPU 编号 " + "、".join(str(value) for value in gpu_ids))
    for path, label in (
        ("target.install_root", "安装目录"),
        ("target.model_root", "模型目录"),
    ):
        if value := _at(intent, path):
            items.append(f"{label} {value}")
    variant = _at(intent, "model.variant")
    if variant == "both":
        items.append("模型的所有变体都需要")
    elif variant:
        items.append(f"模型变体 {variant}")
    if bind_host := _at(intent, "service.bind_host"):
        items.append(f"服务只监听 {bind_host}")
    if source := _at(intent, "preferences.download_source"):
        items.append(f"下载源 {str(source).replace('modelscope', 'ModelScope')}")
    if isolation := _at(intent, "preferences.environment_isolation"):
        isolation_label = {
            "isolated_uv": "独立 uv 环境",
            "isolated_venv": "独立 venv 环境",
            "container": "容器隔离环境",
        }[str(isolation)]
        items.append(f"保留原环境并使用{isolation_label}")
    if region := _at(intent, "deployment_region"):
        items.append(f"部署地区 {region}")
    if intended_use := _at(intent, "intended_use"):
        items.append(f"用途 {intended_use}")
    return items


def summarize_draft(draft: Mapping[str, Any]) -> str:
    readiness = evaluate_draft(draft)
    intent = draft["intent"]
    lines = ["已记住：" + ("；".join(_confirmed_items(intent)) or "暂时还没有部署信息") + "。"]
    if _at(intent, "preferences.download_source") == "modelscope":
        lines.append(
            "ModelScope 来源已经记下；当前执行器还没有对应下载动作，"
            "需要在计划阶段完成适配并审核，不需要你重复选择下载源。"
        )
    if readiness.discovery_status == "PASS":
        lines.append(
            "现在可以先做只读服务器检查（GPU、内存、磁盘和端口）；"
            "严格 SSH known_hosts 校验仍由连接工具强制执行。"
        )
        if readiness.pending_resolution:
            lines.append(
                "框架和服务端口会由工具根据这次只读检查结果确定，"
                "不需要你现在猜或重复填表。"
            )
    else:
        missing = list(readiness.discovery_missing)
        if (
            missing == ["target.host.ssh_username"]
            and _has_selector(intent)
            and _at(intent, "target.host.ssh_port") is not None
        ):
            lines.append("你已经提供了服务器地址和 SSH 端口，现在只缺 SSH 登录用户名。")
        else:
            labels = "、".join(FIELD_LABELS[path] for path in missing)
            lines.append(f"开始只读检查前，现在只需要补充：{labels}。")
    # 白话摘要只说当前下一步；完整缺失字段仍保留在 JSON 状态中供规划工具使用。
    lines.append(
        "真正开始远程安装前，还会检查完整配置和模型许可，"
        "并把明确的安装计划交给你确认；当前草稿不会改动服务器。"
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="持续合并部署需求，并用白话说明真正缺少什么")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="创建需求草稿")
    create.add_argument("--draft-id", required=True)
    create.add_argument("--update", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    merge = subparsers.add_parser("merge", help="把本轮新增信息合并进已有草稿")
    merge.add_argument("--draft", required=True, type=Path)
    merge.add_argument("--update", required=True, type=Path)
    merge.add_argument("--output", type=Path)
    status = subparsers.add_parser("status", help="显示已记住、可推进和仍缺少的内容")
    status.add_argument("--draft", required=True, type=Path)
    status.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            draft = create_draft(args.draft_id, load_document(args.update))
            atomic_write_json(args.output, draft)
        elif args.command == "merge":
            draft = merge_draft(load_document(args.draft), load_document(args.update))
            atomic_write_json(args.output or args.draft, draft)
        else:
            draft = load_document(args.draft)
        if args.command == "status" and args.json:
            result = {
                "readiness": evaluate_draft(draft).as_dict(),
                "summary": summarize_draft(draft),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(summarize_draft(draft))
        return 0
    except (HarnessError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "原因": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
