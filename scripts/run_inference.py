"""调用已审核的模型 API 并生成证据；绝不接受调用方自填的 PASS 状态。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from scripts._common import (
    ROOT,
    HarnessError,
    atomic_write_json,
    canonical_plan_sha256,
    file_sha256,
    load_document,
    validate_instance,
)
from scripts.deployment_archive import DeploymentArchive

OpenUrl = Callable[..., Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checked_endpoint(endpoint: str, plan: dict[str, Any]) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
    ):
        raise HarnessError("端点必须是不含路径的 HTTP(S) 源地址")
    if parsed.port != plan["service"]["port"]:
        raise HarnessError("端点端口与已审核服务不一致")
    allowed = {plan["service"]["bind_host"]}
    if plan["service"]["bind_host"] in {"0.0.0.0", "::"}:
        allowed.update({"127.0.0.1", "localhost", "::1"})
    if parsed.hostname not in allowed:
        raise HarnessError("端点主机与已审核服务不一致")
    return endpoint.rstrip("/") + "/"


def _json_response(
    opener: OpenUrl, request: urllib.request.Request, timeout: float
) -> dict[str, Any]:
    with opener(request, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise HarnessError("模型 API 返回的 JSON 响应不是对象")
    return value


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _run_inference_unrecorded(
    plan: dict[str, Any], recipe: dict[str, Any], endpoint: str, payload_path: Path,
    output_path: Path, response_path: Path, proof_path: Path, *, timeout: float = 1800,
    poll_interval: float = 2, opener: OpenUrl = urllib.request.urlopen,
) -> dict[str, Any]:
    """向已审核端点提交、轮询、下载并记录一次真实推理。"""
    validate_instance(plan, "deployment-plan.schema.json")
    if plan["review"]["plan_sha256"] != canonical_plan_sha256(plan):
        raise HarnessError("计划哈希与已批准审核不一致")
    if recipe.get("model_id") != plan["model"]["id"]:
        raise HarnessError("验证配方与模型不一致")
    reviewed_recipe_path = (ROOT / plan["verification"]["recipe_ref"]).resolve()
    if (
        not reviewed_recipe_path.is_relative_to((ROOT / "models").resolve())
        or not reviewed_recipe_path.is_file()
        or recipe != load_document(reviewed_recipe_path)
    ):
        raise HarnessError("验证配方内容与已审核配方不一致")
    if any(path.exists() for path in (output_path, response_path, proof_path)):
        raise HarnessError("推理证据目标文件不得已存在")
    payload_bytes = payload_path.read_bytes()
    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict) or not payload:
        raise HarnessError("推理载荷必须为非空 JSON 对象")

    api = recipe["inference_api"]
    origin = _checked_endpoint(endpoint, plan)
    submitted_at = _now()
    submit = urllib.request.Request(
        urljoin(origin, api["submit_path"].lstrip("/")), data=payload_bytes,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    initial = _json_response(opener, submit, min(timeout, 60))
    job_id = str(initial.get("id", ""))
    if not job_id:
        raise HarnessError("模型 API 提交响应未包含任务 ID")

    deadline = time.monotonic() + timeout
    final = initial
    while str(final.get("status", "")).upper() not in {"COMPLETED", "FAILED"}:
        if time.monotonic() >= deadline:
            raise HarnessError("模型推理超时")
        if poll_interval:
            time.sleep(poll_interval)
        status_path = api["status_path_template"].format(id=quote(job_id, safe=""))
        final = _json_response(
            opener,
            urllib.request.Request(urljoin(origin, status_path.lstrip("/")), method="GET"),
            min(max(deadline - time.monotonic(), 1), 60),
        )
        if str(final.get("id", "")) != job_id:
            raise HarnessError("模型 API 状态响应变更了任务 ID")
    if str(final.get("status", "")).upper() != "COMPLETED":
        raise HarnessError("模型推理任务失败")

    content_path = api["content_path_template"].format(id=quote(job_id, safe=""))
    with opener(
        urllib.request.Request(urljoin(origin, content_path.lstrip("/")), method="GET"),
        timeout=min(max(deadline - time.monotonic(), 1), 300),
    ) as response:
        media = response.read()
    if not media:
        raise HarnessError("模型 API 返回了空推理输出")
    _atomic_write_bytes(output_path, media)
    atomic_write_json(response_path, final)
    proof = {
        "schema_version": "1.0",
        "producer": "HARNESS_HTTP_RUNNER",
        "deployment_id": plan["deployment_id"],
        "plan_sha256": plan["review"]["plan_sha256"],
        "endpoint": endpoint.rstrip("/"),
        "request": {
            "method": "POST", "path": api["submit_path"],
            "payload": {
                "path": str(payload_path), "sha256": file_sha256(payload_path),
                "media_type": "application/json",
            },
            "submitted_at": submitted_at,
        },
        "job": {
            "job_id": job_id, "status": "COMPLETED", "completed_at": _now(),
            "response": {
                "path": str(response_path), "sha256": file_sha256(response_path),
                "media_type": "application/json",
            },
            "runtime_error": None,
        },
        "output": {
            "path": str(output_path), "sha256": file_sha256(output_path),
            "media_type": "video/mp4",
        },
    }
    validate_instance(proof, "inference-proof.schema.json")
    atomic_write_json(proof_path, proof)
    return proof


def run_inference(
    plan: dict[str, Any], recipe: dict[str, Any], endpoint: str, payload_path: Path,
    output_path: Path, response_path: Path, proof_path: Path, *, timeout: float = 1800,
    poll_interval: float = 2, opener: OpenUrl = urllib.request.urlopen,
    archive: DeploymentArchive | None = None,
) -> dict[str, Any]:
    """执行真实推理，并把成功或失败尝试自动写入部署档案。"""
    try:
        proof = _run_inference_unrecorded(
            plan,
            recipe,
            endpoint,
            payload_path,
            output_path,
            response_path,
            proof_path,
            timeout=timeout,
            poll_interval=poll_interval,
            opener=opener,
        )
    except Exception as exc:
        if archive is not None:
            target = plan.get("target")
            review = plan.get("review")
            existing_artifacts = tuple(
                path
                for path in (payload_path, response_path, output_path, proof_path)
                if path.is_file()
            )
            archive.record(
                stage="INFERENCE",
                status="BLOCKED",
                summary="真实推理未能完成，已保留现有证据",
                host_id=(
                    str(target["host_id"])
                    if isinstance(target, dict) and target.get("host_id")
                    else None
                ),
                artifacts=existing_artifacts,
                details={
                    "error_type": exc.__class__.__name__,
                    "endpoint": endpoint.rstrip("/"),
                    "plan_sha256": (
                        review.get("plan_sha256") if isinstance(review, dict) else None
                    ),
                },
            )
        raise
    if archive is not None:
        archive.record(
            stage="INFERENCE",
            status="PASS",
            summary="真实推理已完成，请求、响应、输出和证明均已收录",
            host_id=str(plan["target"]["host_id"]),
            artifacts=[payload_path, response_path, output_path, proof_path],
            details={
                "job_id": proof["job"]["job_id"],
                "endpoint": endpoint.rstrip("/"),
                "plan_sha256": plan["review"]["plan_sha256"],
            },
        )
    return proof


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行一项已审核的真实推理验证任务")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--request-payload", required=True, type=Path)
    parser.add_argument("--media-output", required=True, type=Path)
    parser.add_argument("--response-output", required=True, type=Path)
    parser.add_argument("--proof-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_document(args.plan)
        expected_recipe = (
            ROOT / plan["verification"]["recipe_ref"]
        ).resolve()
        model_root = (ROOT / "models").resolve()
        if (
            not expected_recipe.is_relative_to(model_root)
            or args.recipe.resolve() != expected_recipe
        ):
            raise HarnessError("--recipe 与已审核计划的 recipe_ref 不一致")
        run_inference(
            plan, load_document(args.recipe), args.endpoint,
            args.request_payload, args.media_output, args.response_output, args.proof_output,
            archive=DeploymentArchive(plan["deployment_id"]),
        )
        print(f"COMPLETED: {args.proof_output}")
        return 0
    except (HarnessError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
