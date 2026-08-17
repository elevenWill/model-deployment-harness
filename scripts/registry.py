from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts._common import (
        ROOT,
        HarnessError,
        atomic_write_json,
        load_document,
        safe_identifier,
        validate_instance,
    )
    from scripts.probe_host import ParamikoTransport, collect_host_profile
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from _common import (  # type: ignore[no-redef]
        ROOT,
        HarnessError,
        atomic_write_json,
        load_document,
        safe_identifier,
        validate_instance,
    )
    from probe_host import ParamikoTransport, collect_host_profile  # type: ignore[no-redef]


SCHEMA_BY_KIND = {
    "deployment": "deployment-record.schema.json",
    "incident": "incident.schema.json",
    "lesson": "lesson.schema.json",
    "decision": "decision-record.schema.json",
    "benchmark": "benchmark.schema.json",
}
DIRECTORY_BY_KIND = {
    "deployment": ROOT / "deployments",
    "incident": ROOT / "knowledge" / "incidents",
    "lesson": ROOT / "knowledge" / "lessons",
    "decision": ROOT / "knowledge" / "decisions",
    "benchmark": ROOT / "knowledge" / "benchmarks",
}
ID_BY_KIND = {
    "deployment": "deployment_id",
    "incident": "incident_id",
    "lesson": "lesson_id",
    "decision": "decision_id",
    "benchmark": "benchmark_id",
}


def record_artifact(kind: str, document: dict[str, Any]) -> Path:
    validate_instance(document, SCHEMA_BY_KIND[kind])
    artifact_id = safe_identifier(str(document[ID_BY_KIND[kind]]), ID_BY_KIND[kind])
    destination = DIRECTORY_BY_KIND[kind] / f"{artifact_id}.json"
    atomic_write_json(destination, document)
    return destination


def upsert_host(registry_path: Path, host_entry: dict[str, Any], updated_at: str) -> Path:
    host_id = safe_identifier(str(host_entry.get("host_id", "")), "host_id")
    if registry_path.exists():
        registry = load_document(registry_path)
    else:
        registry = {"schema_version": "1.0", "updated_at": updated_at, "hosts": []}
    hosts = registry.setdefault("hosts", [])
    if not isinstance(hosts, list):
        raise HarnessError("registry.hosts 必须为数组")
    for index, existing in enumerate(hosts):
        if isinstance(existing, dict) and existing.get("host_id") == host_id:
            hosts[index] = host_entry
            break
    else:
        hosts.append(host_entry)
    registry["updated_at"] = updated_at
    validate_instance(registry, "host-registry.schema.json")
    atomic_write_json(registry_path, registry)
    return registry_path


def attach_observation(record: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["observed_state"] = observation
    validate_instance(result, "deployment-record.schema.json")
    return result


def deployment_status_view(record: dict[str, Any]) -> dict[str, Any]:
    known = record["known_state"]
    observed = record.get("observed_state")
    live_summary: str
    if observed is None:
        live_summary = "NOT_CHECKED：历史已知状态不是当前状态"
    else:
        failures = [
            name for name in ("ssh", "process", "port", "api", "inference")
            if observed.get(name) in {"FAIL", "BLOCKED"}
        ]
        incomplete = [
            name
            for name in ("ssh", "process", "port", "api", "inference")
            if observed.get(name) == "NOT_CHECKED"
        ]
        if failures:
            live_summary = "已知状态 ≠ 观测状态：" + "，".join(failures)
        elif incomplete:
            live_summary = "INCOMPLETE/NOT_CHECKED: " + ", ".join(incomplete)
        else:
            live_summary = "OK"
    return {
        "deployment_id": record["deployment_id"],
        "registry_says": {
            "expected_service_state": known["expected_service_state"],
            "expected_port": known["expected_port"],
            "recorded_at": known["recorded_at"],
        },
        "live_check": observed,
        "live_summary": live_summary,
        "last_full_verification": known.get("last_full_verification_at"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验并存储工具注册表制品")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for kind in SCHEMA_BY_KIND:
        command = subparsers.add_parser(f"record-{kind}")
        command.add_argument("--file", required=True, type=Path)
    show = subparsers.add_parser("show-deployment")
    show.add_argument("--deployment-id", required=True)
    observation_group = show.add_mutually_exclusive_group()
    observation_group.add_argument("--observation", type=Path)
    observation_group.add_argument("--live", action="store_true")
    show.add_argument("--host")
    show.add_argument("--username")
    show.add_argument("--port", type=int, default=22)
    show.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    show.add_argument(
        "--registry", type=Path, default=ROOT / "inventory" / "hosts" / "registry.json"
    )
    show_host = subparsers.add_parser("show-host")
    show_host.add_argument("--host-id", required=True)
    show_host.add_argument("--live", action="store_true")
    show_host.add_argument("--host")
    show_host.add_argument("--username")
    show_host.add_argument("--port", type=int, default=22)
    show_host.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    show_host.add_argument(
        "--registry", type=Path, default=ROOT / "inventory" / "hosts" / "registry.json"
    )
    host = subparsers.add_parser("upsert-host")
    host.add_argument("--entry", required=True, type=Path)
    host.add_argument("--updated-at", required=True)
    host.add_argument(
        "--registry", type=Path, default=ROOT / "inventory" / "hosts" / "registry.json"
    )
    return parser


def _probe_live(
    host_id: str, host: str, username: str, port: int, env_file: Path
) -> dict[str, Any]:
    transport = None
    try:
        transport = ParamikoTransport.connect(
            host, username=username, port=port, dotenv_path=env_file
        )
        profile = collect_host_profile(transport, host_id=host_id)
        validate_instance(profile, "host-profile.schema.json")
        return profile
    except Exception as exc:
        raise HarnessError(f"实时 SSH 探测失败：{exc.__class__.__name__}") from exc
    finally:
        if transport is not None:
            transport.close()


def _host_entry(registry_path: Path, host_id: str) -> dict[str, Any]:
    registry = load_document(registry_path)
    validate_instance(registry, "host-registry.schema.json")
    entry = next((item for item in registry["hosts"] if item["host_id"] == host_id), None)
    if entry is None:
        raise HarnessError(f"未知 host_id：{host_id}")
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command.startswith("record-"):
            kind = args.command.removeprefix("record-")
            print(record_artifact(kind, load_document(args.file)))
        elif args.command == "upsert-host":
            print(upsert_host(args.registry, load_document(args.entry), args.updated_at))
        elif args.command == "show-deployment":
            deployment_id = safe_identifier(args.deployment_id, "deployment_id")
            record_path = DIRECTORY_BY_KIND["deployment"] / f"{deployment_id}.json"
            record = load_document(record_path)
            validate_instance(record, "deployment-record.schema.json")
            if args.observation:
                record = attach_observation(record, load_document(args.observation))
            elif args.live:
                entry = _host_entry(args.registry, record["host_id"])
                endpoint = args.host or entry["addresses"][0]
                if not args.username:
                    raise HarnessError("使用 --live 时必须提供 --username")
                try:
                    profile = _probe_live(
                        record["host_id"], endpoint, args.username, args.port, args.env_file
                    )
                except HarnessError:
                    observation = {
                        "checked_at": _now(),
                        "ssh": "FAIL",
                        "process": "NOT_CHECKED",
                        "port": "NOT_CHECKED",
                        "api": "NOT_CHECKED",
                        "inference": "NOT_CHECKED",
                    }
                else:
                    service_process = any(
                        record["target"]["port"] in service["ports"]
                        for service in profile["runtime"]["model_services"]
                    )
                    port_open = (
                        record["target"]["port"] in profile["network"]["listening_ports"]
                    )
                    observation = {
                        "checked_at": profile["observed_at"],
                        "ssh": "PASS",
                        "process": "PASS" if service_process else "FAIL",
                        "port": "PASS" if port_open else "FAIL",
                        "api": "NOT_CHECKED",
                        "inference": "NOT_CHECKED",
                        "host_profile_ref": "live://in-memory",
                    }
                record = attach_observation(record, observation)
            print(json.dumps(deployment_status_view(record), indent=2, ensure_ascii=False))
        else:
            host_id = safe_identifier(args.host_id, "host_id")
            entry = _host_entry(args.registry, host_id)
            live_profile = None
            if args.live:
                if not args.username:
                    raise HarnessError("使用 --live 时必须提供 --username")
                endpoint = args.host or entry["addresses"][0]
                try:
                    live_profile = _probe_live(
                        host_id, endpoint, args.username, args.port, args.env_file
                    )
                except HarnessError:
                    live_profile = {
                        "checked_at": _now(),
                        "probe_status": "FAILED",
                        "detail": "实时 SSH 探测失败",
                    }
            view = {
                "host_id": host_id,
                "known_state": entry["known_state"],
                "observed_state": live_profile or entry.get("observed_state"),
                "warning": (
                    None
                    if live_profile and live_profile.get("probe", {}).get("status") == "COMPLETE"
                    else "实时检查失败/未检查：已知状态仍为历史数据"
                ),
            }
            print(json.dumps(view, indent=2, ensure_ascii=False))
        return 0
    except (HarnessError, OSError, ValueError) as exc:
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
