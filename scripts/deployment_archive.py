"""将整个部署生命周期收敛到一份可校验、可追溯的本地档案。"""

from __future__ import annotations

import fcntl
import mimetypes
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts._common import (
    ROOT,
    HarnessError,
    atomic_write_json,
    file_sha256,
    load_document,
    safe_identifier,
    validate_instance,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise HarnessError(f"档案制品不存在：{path}")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {"path": str(path), "sha256": file_sha256(path), "media_type": media_type}


class DeploymentArchive:
    """单一记录接口：追加事件、绑定制品并原子更新档案。"""

    def __init__(
        self,
        deployment_id: str,
        *,
        root: Path = ROOT / "deployments",
        knowledge_root: Path = ROOT / "knowledge",
    ) -> None:
        self.deployment_id = safe_identifier(deployment_id, "deployment_id")
        self.directory = root / self.deployment_id
        self.path = self.directory / "archive.json"
        self._lock_path = self.directory / ".archive.lock"
        self.knowledge_root = knowledge_root

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def record(
        self,
        *,
        stage: str,
        status: str,
        summary: str,
        artifacts: Sequence[Path] = (),
        host_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> Path:
        timestamp = occurred_at or _now()
        references = [_artifact(Path(path)) for path in artifacts]
        with self._locked():
            recorded_knowledge: list[Path] = []
            if self.path.exists():
                archive = load_document(self.path)
                validate_instance(archive, "deployment-archive.schema.json")
                if archive["deployment_id"] != self.deployment_id:
                    raise HarnessError("档案 deployment_id 与目录不一致")
            else:
                archive = {
                    "schema_version": "1.0",
                    "deployment_id": self.deployment_id,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "events": [],
                }
            sequence = len(archive["events"]) + 1
            event_details = dict(details or {})
            if stage == "EXECUTE":
                execution_path = self._write_execution_record(
                    sequence, status, host_id, event_details, timestamp
                )
                references.append(_artifact(execution_path))
                incident_path: Path | None = None
                if status in {"BLOCKED", "FAILED"}:
                    incident_path = self._write_incident(
                        sequence, host_id, event_details, timestamp, execution_path
                    )
                    references.append(_artifact(incident_path))
                if isinstance(event_details.get("deployment"), Mapping):
                    deployment_path = self._write_deployment_record(
                        status,
                        host_id,
                        event_details["deployment"],
                        timestamp,
                        incident_path,
                    )
                    references.append(_artifact(deployment_path))
                event_details = {
                    "plan_sha256": event_details.get("plan_sha256"),
                    "step_count": len(event_details.get("steps", [])),
                    "blocker": event_details.get("blocker"),
                }
                event_details = {
                    key: value for key, value in event_details.items() if value is not None
                }
            elif stage == "VERIFY" and status in {"VERIFIED", "FAILED", "INCOMPLETE"}:
                deployment_path, benchmark_path = self._write_verification_knowledge(
                    sequence, status, host_id, event_details, timestamp
                )
                references.append(_artifact(deployment_path))
                recorded_knowledge.append(deployment_path)
                if benchmark_path is not None:
                    references.append(_artifact(benchmark_path))
                    recorded_knowledge.append(benchmark_path)
            event: dict[str, Any] = {
                "event_id": f"{self.deployment_id}-{sequence:04d}",
                "sequence": sequence,
                "occurred_at": timestamp,
                "stage": stage,
                "status": status,
                "summary": summary,
                "artifacts": references,
            }
            if host_id:
                event["host_id"] = host_id
            if event_details:
                event["details"] = event_details
            archive["events"].append(event)
            if recorded_knowledge:
                record_sequence = sequence + 1
                record_event: dict[str, Any] = {
                    "event_id": f"{self.deployment_id}-{record_sequence:04d}",
                    "sequence": record_sequence,
                    "occurred_at": timestamp,
                    "stage": "RECORD",
                    "status": "RECORDED",
                    "summary": "部署状态与经验证知识已自动登记",
                    "artifacts": [_artifact(path) for path in recorded_knowledge],
                    "details": {"source_event_id": event["event_id"]},
                }
                if host_id:
                    record_event["host_id"] = host_id
                archive["events"].append(record_event)
            archive["updated_at"] = timestamp
            validate_instance(archive, "deployment-archive.schema.json")
            atomic_write_json(self.path, archive)
        return self.path

    def _write_execution_record(
        self,
        sequence: int,
        status: str,
        host_id: str | None,
        details: Mapping[str, Any],
        timestamp: str,
    ) -> Path:
        if not host_id:
            raise HarnessError("执行记录必须包含 host_id")
        steps: list[dict[str, Any]] = []
        for source in details.get("steps", []):
            if not isinstance(source, Mapping):
                raise HarnessError("执行步骤必须为对象")
            stdout, stdout_cut = _bounded_text(source.get("stdout", ""))
            stderr, stderr_cut = _bounded_text(source.get("stderr", ""))
            step = {
                "step_id": source.get("step_id"),
                "started_at": source.get("started_at"),
                "completed_at": source.get("completed_at"),
                "returncode": source.get("returncode"),
                "stdout_redacted": stdout,
                "stderr_redacted": stderr,
            }
            if stdout_cut or stderr_cut:
                step["output_truncated"] = True
            steps.append(step)
        record: dict[str, Any] = {
            "schema_version": "1.0",
            "producer": "HARNESS_PLAN_EXECUTOR",
            "execution_id": f"execution-{self.deployment_id}-{sequence:04d}",
            "deployment_id": self.deployment_id,
            "host_id": host_id,
            "plan_sha256": details.get("plan_sha256"),
            "started_at": details.get("started_at", timestamp),
            "completed_at": details.get("completed_at", timestamp),
            "status": status,
            "steps": steps,
        }
        if details.get("blocker"):
            record["blocker"] = str(details["blocker"])
        validate_instance(record, "execution-record.schema.json")
        path = self.directory / f"execution-{sequence:04d}.json"
        atomic_write_json(path, record)
        return path

    def _write_incident(
        self,
        sequence: int,
        host_id: str | None,
        details: Mapping[str, Any],
        timestamp: str,
        execution_path: Path,
    ) -> Path:
        if not host_id:
            raise HarnessError("事故记录必须包含 host_id")
        blocker = str(details.get("blocker") or "远程执行失败")
        incident_id = f"incident-{self.deployment_id}-{sequence:04d}"
        incident = {
            "schema_version": "1.0",
            "incident_id": incident_id,
            "deployment_id": self.deployment_id,
            "host_id": host_id,
            "opened_at": timestamp,
            "severity": "HIGH",
            "symptom": blocker,
            "environment": str(details.get("environment") or f"host_id={host_id}"),
            "status": "OPEN",
            "timeline": [
                {
                    "at": timestamp,
                    "event": blocker,
                    "actor": "model-deployment-harness",
                }
            ],
            "artifacts": [_artifact(execution_path)],
        }
        validate_instance(incident, "incident.schema.json")
        path = self.knowledge_root / "incidents" / f"{incident_id}.json"
        atomic_write_json(path, incident)
        return path

    def _write_deployment_record(
        self,
        execution_status: str,
        host_id: str | None,
        context: Mapping[str, Any],
        timestamp: str,
        incident_path: Path | None,
    ) -> Path:
        if not host_id:
            raise HarnessError("部署记录必须包含 host_id")
        path = self.directory.parent / f"{self.deployment_id}.json"
        if path.exists():
            record = load_document(path)
            validate_instance(record, "deployment-record.schema.json")
        else:
            record = {
                "schema_version": "1.0",
                "deployment_id": self.deployment_id,
                "host_id": host_id,
                "request_ref": str(context["request_ref"]),
                "plan_ref": str(context["plan_ref"]),
                "model": dict(context["model"]),
                "framework": dict(context["framework"]),
                "target": dict(context["target"]),
                "deployment_status": "UNKNOWN",
                "created_at": timestamp,
                "updated_at": timestamp,
                "known_state": {
                    "recorded_at": timestamp,
                    "expected_service_state": "UNKNOWN",
                    "expected_port": context["target"]["port"],
                },
            }
        record["deployment_status"] = (
            "STARTED" if execution_status == "EXECUTED" else "FAILED"
        )
        record["updated_at"] = timestamp
        record["known_state"].update(
            {
                "recorded_at": timestamp,
                "expected_service_state": (
                    "RUNNING" if execution_status == "EXECUTED" else "UNKNOWN"
                ),
            }
        )
        if incident_path is not None:
            refs = record.setdefault("incident_refs", [])
            incident_id = incident_path.stem
            if incident_id not in refs:
                refs.append(incident_id)
        validate_instance(record, "deployment-record.schema.json")
        atomic_write_json(path, record)
        return path

    def _write_verification_knowledge(
        self,
        sequence: int,
        status: str,
        host_id: str | None,
        details: Mapping[str, Any],
        timestamp: str,
    ) -> tuple[Path, Path | None]:
        path = self.directory.parent / f"{self.deployment_id}.json"
        if not path.is_file():
            deployment = details.get("deployment")
            if not isinstance(deployment, Mapping):
                raise HarnessError("验证前缺少自动部署记录")
            self._write_deployment_record(
                "EXECUTED", host_id, deployment, timestamp, None
            )
        record = load_document(path)
        validate_instance(record, "deployment-record.schema.json")
        if host_id and record["host_id"] != host_id:
            raise HarnessError("验证 host_id 与部署记录不一致")
        verification_ref = str(details.get("verification_ref", ""))
        if not verification_ref:
            raise HarnessError("验证事件缺少 verification_ref")
        record["deployment_status"] = (
            "VERIFIED" if status == "VERIFIED" else "VERIFICATION_FAILED"
        )
        record["updated_at"] = timestamp
        record["known_state"].update(
            {
                "recorded_at": timestamp,
                "expected_service_state": "RUNNING",
                "verification_ref": verification_ref,
            }
        )
        benchmark_path: Path | None = None
        if status == "VERIFIED":
            record["known_state"]["last_full_verification_at"] = timestamp
            benchmark_id = f"benchmark-{self.deployment_id}-{sequence:04d}"
            metrics = {
                str(key): value
                for key, value in dict(details.get("metrics", {})).items()
                if value is not None and isinstance(value, (int, float, str))
            }
            benchmark = {
                "schema_version": "1.0",
                "benchmark_id": benchmark_id,
                "deployment_id": self.deployment_id,
                "host_id": record["host_id"],
                "recorded_at": timestamp,
                "workload": dict(details.get("workload", {})),
                "environment": dict(details.get("environment", {})),
                "metrics": metrics,
                "verification_ref": verification_ref,
            }
            validate_instance(benchmark, "benchmark.schema.json")
            benchmark_path = (
                self.knowledge_root / "benchmarks" / f"{benchmark_id}.json"
            )
            atomic_write_json(benchmark_path, benchmark)
            refs = record.setdefault("benchmark_refs", [])
            if benchmark_id not in refs:
                refs.append(benchmark_id)
        validate_instance(record, "deployment-record.schema.json")
        atomic_write_json(path, record)
        return path, benchmark_path


def _bounded_text(value: object, limit: int = 65536) -> tuple[str, bool]:
    text = str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[输出已截断]", True
