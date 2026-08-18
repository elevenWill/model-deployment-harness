"""调用已审核的模型 API 并生成证据；绝不接受调用方自填的 PASS 状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
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
    assert_no_secrets,
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


def _atomic_create_bytes(path: Path, value: bytes) -> None:
    """Atomically create a new harness artifact without replacing any path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise HarnessError(f"推理证据目标文件已存在：{path}") from exc
        Path(temporary).unlink()
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def generated_request_path(proof_path: Path) -> Path:
    """Return the harness-owned artifact path for the exact submitted request."""
    return proof_path.with_name(f"{proof_path.stem}.generated-request.json")


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise HarnessError("输出引用必须使用以 / 开头的 JSON Pointer")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise HarnessError(f"完成响应缺少配方声明的输出字段：{pointer}")
    return current


def _set_json_pointer(document: dict[str, Any], pointer: str, value: str) -> None:
    if not pointer.startswith("/"):
        raise HarnessError("请求 token 字段必须使用以 / 开头的 JSON Pointer")
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if not tokens:
        raise HarnessError("请求 token JSON Pointer 不得指向文档根")
    current: Any = document
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            raise HarnessError(f"请求缺少配方声明的输出命名字段：{pointer}")
        current = current[token]
    if not isinstance(current, dict) or tokens[-1] not in current:
        raise HarnessError(f"请求缺少配方声明的输出命名字段：{pointer}")
    current[tokens[-1]] = value


def _comfyui_output_reference(
    history: dict[str, Any], prompt_id: str, api: dict[str, Any]
) -> tuple[dict[str, str], str, str, str, int]:
    """Extract one MP4 output from the documented ComfyUI prompt-history response."""
    entry = history.get(prompt_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("outputs"), dict):
        raise HarnessError("ComfyUI history 尚未包含带 outputs 的已完成任务")
    item_keys = set(api.get("output_item_keys", ("gifs", "videos")))
    extension = str(api.get("output_extension", ".mp4")).lower()
    candidates: list[tuple[dict[str, str], str, str, str, int]] = []
    for node_id, node in entry["outputs"].items():
        if not isinstance(node, dict):
            continue
        for item_key in item_keys:
            values = node.get(item_key)
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if not isinstance(value, dict):
                    continue
                filename = value.get("filename")
                subfolder = value.get("subfolder", "")
                output_type = value.get("type", "output")
                if (
                    isinstance(filename, str)
                    and filename.lower().endswith(extension)
                    and isinstance(subfolder, str)
                    and isinstance(output_type, str)
                ):
                    candidates.append(
                        (
                            {"filename": filename, "subfolder": subfolder, "type": output_type},
                            "/history/"
                            + _pointer_token(prompt_id)
                            + "/outputs/"
                            + _pointer_token(str(node_id))
                            + "/"
                            + _pointer_token(str(item_key))
                            + f"/{index}",
                            str(node_id),
                            str(item_key),
                            index,
                        )
                    )
    if len(candidates) != 1:
        raise HarnessError("ComfyUI 已完成工作流必须恰好产出一个 MP4；请使用已审核的单输出工作流")
    return candidates[0]


def resolve_output_binding(
    response: dict[str, Any],
    job_id: str,
    api: dict[str, Any],
    origin: str,
    request_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the output solely from the reviewed recipe and raw completed response."""
    mapping = api.get("output_reference")
    if not isinstance(mapping, dict):
        raise HarnessError("已审核验证配方缺少 output_reference 输出来源映射")
    resolver = mapping.get("resolver")
    if resolver == "json_pointer_fields":
        artifact_pointer = mapping.get("artifact_id_pointer")
        url_pointer = mapping.get("url_pointer")
        if not isinstance(artifact_pointer, str) or not isinstance(url_pointer, str):
            raise HarnessError("output_reference 必须声明产物 ID 和 URL 的 JSON Pointer")
        artifact_id = _json_pointer(response, artifact_pointer)
        raw_url = _json_pointer(response, url_pointer)
        if not isinstance(artifact_id, str) or not artifact_id:
            raise HarnessError("完成响应中的输出产物 ID 无效")
        if not isinstance(raw_url, str) or not raw_url:
            raise HarnessError("完成响应中的输出 URL 无效")
        binding = {
            "resolver": "RECIPE_JSON_POINTER_FIELDS",
            "job_id": job_id,
            "response_artifact_pointer": artifact_pointer,
            "response_url_pointer": url_pointer,
            "artifact_id": artifact_id,
            "content_url": urljoin(origin, raw_url),
        }
        sha_pointer = mapping.get("content_sha256_pointer")
        if not isinstance(sha_pointer, str):
            raise HarnessError("output_reference 必须声明内容 SHA-256 的 JSON Pointer")
        declared_hash = _json_pointer(response, sha_pointer)
        if (
            not isinstance(declared_hash, str)
            or len(declared_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in declared_hash)
        ):
            raise HarnessError("完成响应中的输出 SHA-256 无效")
        binding["response_sha256_pointer"] = sha_pointer
        binding["response_content_sha256"] = declared_hash.lower()
    elif resolver == "comfyui_output_item":
        history = response.get("history")
        if not isinstance(history, dict):
            raise HarnessError("ComfyUI 完成响应缺少原始 history")
        item, item_pointer, node_id, output_collection, output_index = (
            _comfyui_output_reference(history, job_id, api)
        )
        token_rule = mapping.get("job_token")
        if not isinstance(token_rule, dict) or request_payload is None:
            raise HarnessError("ComfyUI 输出绑定缺少请求 job token 规则或原始请求")
        token_pointer = token_rule.get("request_pointer")
        if not isinstance(token_pointer, str):
            raise HarnessError("ComfyUI job token 规则缺少请求 JSON Pointer")
        job_token = _json_pointer(request_payload, token_pointer)
        if not isinstance(job_token, str) or len(job_token) < 32:
            raise HarnessError("ComfyUI 请求 job token 无效或强度不足")
        if not item["filename"].startswith(job_token):
            raise HarnessError("ComfyUI history 输出文件名未绑定本次请求的唯一 job token")
        content_path = api["content_path_template"].format(
            filename=quote(item["filename"], safe=""),
            subfolder=quote(item["subfolder"], safe="/"),
            type=quote(item["type"], safe=""),
        )
        artifact_id = f'{item["type"]}:{item["subfolder"]}/{item["filename"]}'
        binding = {
            "resolver": "RECIPE_OUTPUT_ITEM",
            "job_id": job_id,
            "response_artifact_pointer": item_pointer,
            "artifact_id": artifact_id,
            "content_url": urljoin(origin, content_path.lstrip("/")),
            "request_token_pointer": token_pointer,
            "job_token": job_token,
            "filename": item["filename"],
            "subfolder": item["subfolder"],
            "output_type": item["type"],
            "output_node_id": node_id,
            "output_collection": output_collection,
            "output_index": output_index,
        }
    else:
        raise HarnessError("output_reference.resolver 不受支持")

    content = urlparse(binding["content_url"])
    expected = urlparse(origin)
    if (
        content.scheme not in {"http", "https"}
        or not content.hostname
        or (content.scheme, content.hostname, content.port)
        != (expected.scheme, expected.hostname, expected.port)
    ):
        raise HarnessError("完成响应指向了已审核服务源地址以外的输出")
    return binding


def _run_inference_unrecorded(
    plan: dict[str, Any],
    recipe: dict[str, Any],
    endpoint: str,
    payload_path: Path,
    output_path: Path,
    response_path: Path,
    proof_path: Path,
    *,
    timeout: float = 1800,
    poll_interval: float = 2,
    opener: OpenUrl = urllib.request.urlopen,
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
    submitted_request_path = generated_request_path(proof_path)
    evidence_targets = (output_path, response_path, proof_path, submitted_request_path)
    if len({path.resolve() for path in (*evidence_targets, payload_path)}) != 5:
        raise HarnessError("调用方请求文件与推理证据目标路径必须彼此独立")
    if any(os.path.lexists(path) for path in evidence_targets):
        raise HarnessError("推理证据目标文件不得已存在")
    payload = json.loads(payload_path.read_bytes())
    if not isinstance(payload, dict) or not payload:
        raise HarnessError("推理载荷必须为非空 JSON 对象")

    api = recipe["inference_api"]
    protocol = api.get("protocol", "async_job")
    if protocol == "comfyui_prompt_history":
        token_rule = api.get("output_reference", {}).get("job_token")
        if not isinstance(token_rule, dict) or not isinstance(
            token_rule.get("request_pointer"), str
        ):
            raise HarnessError("ComfyUI 配方必须声明唯一输出名的请求 job token 字段")
        token_prefix = token_rule.get("prefix", "harness-")
        if not isinstance(token_prefix, str):
            raise HarnessError("ComfyUI job token 前缀无效")
        _set_json_pointer(
            payload,
            token_rule["request_pointer"],
            token_prefix + secrets.token_hex(16),
        )
    assert_no_secrets(payload)
    payload_bytes = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_create_bytes(submitted_request_path, payload_bytes)
    origin = _checked_endpoint(endpoint, plan)
    submitted_at = _now()
    submit = urllib.request.Request(
        urljoin(origin, api["submit_path"].lstrip("/")),
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    initial = _json_response(opener, submit, min(timeout, 60))
    job_id = str(initial.get("prompt_id" if protocol == "comfyui_prompt_history" else "id", ""))
    if not job_id:
        raise HarnessError("模型 API 提交响应未包含任务 ID")

    deadline = time.monotonic() + timeout
    final = initial
    while True:
        if protocol == "comfyui_prompt_history":
            completed_history = isinstance(final.get(job_id), dict) and isinstance(
                final[job_id].get("outputs"), dict
            )
            if completed_history:
                _comfyui_output_reference(final, job_id, api)
                final = {"id": job_id, "status": "COMPLETED", "history": final}
                break
        elif str(final.get("status", "")).upper() in {"COMPLETED", "FAILED"}:
            break
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
        if protocol != "comfyui_prompt_history" and str(final.get("id", "")) != job_id:
            raise HarnessError("模型 API 状态响应变更了任务 ID")
    if str(final.get("status", "")).upper() != "COMPLETED":
        raise HarnessError("模型推理任务失败")

    job_completed_at = _now()
    output_binding = resolve_output_binding(final, job_id, api, origin, payload)
    download_started_at = _now()
    with opener(
        urllib.request.Request(output_binding["content_url"], method="GET"),
        timeout=min(max(deadline - time.monotonic(), 1), 300),
    ) as response:
        media = response.read()
        response_headers = getattr(response, "headers", None)
        header_values = {
            key: value
            for key, value in (
                ("etag", response_headers.get("ETag") if response_headers is not None else None),
                (
                    "last_modified",
                    response_headers.get("Last-Modified") if response_headers is not None else None,
                ),
                (
                    "content_length",
                    response_headers.get("Content-Length")
                    if response_headers is not None
                    else None,
                ),
            )
            if isinstance(value, str) and value
        }
    if not media:
        raise HarnessError("模型 API 返回了空推理输出")
    media_hash = hashlib.sha256(media).hexdigest()
    declared_hash = output_binding.get("response_content_sha256")
    if declared_hash is not None and declared_hash != media_hash:
        raise HarnessError("下载内容的 SHA-256 与完成响应声明的输出不一致")
    if "content_length" in header_values:
        try:
            declared_length = int(header_values["content_length"])
        except ValueError as exc:
            raise HarnessError("输出响应的 Content-Length 无效") from exc
        if declared_length != len(media):
            raise HarnessError("下载内容长度与输出响应的 Content-Length 不一致")
    output_binding["downloaded_at"] = download_started_at
    output_binding["download_sha256"] = media_hash
    output_binding["download_content_length"] = str(len(media))
    output_binding["response_headers"] = header_values
    _atomic_write_bytes(output_path, media)
    atomic_write_json(response_path, final)
    proof = {
        "schema_version": "1.0",
        "producer": "HARNESS_HTTP_RUNNER",
        "deployment_id": plan["deployment_id"],
        "plan_sha256": plan["review"]["plan_sha256"],
        "endpoint": endpoint.rstrip("/"),
        "request": {
            "method": "POST",
            "path": api["submit_path"],
            "payload": {
                "path": str(submitted_request_path),
                "sha256": file_sha256(submitted_request_path),
                "media_type": "application/json",
            },
            "submitted_at": submitted_at,
        },
        "job": {
            "job_id": job_id,
            "status": "COMPLETED",
            "completed_at": job_completed_at,
            "response": {
                "path": str(response_path),
                "sha256": file_sha256(response_path),
                "media_type": "application/json",
            },
            "runtime_error": None,
        },
        "output_binding": output_binding,
        "output": {
            "path": str(output_path),
            "sha256": file_sha256(output_path),
            "media_type": "video/mp4",
        },
    }
    validate_instance(proof, "inference-proof.schema.json")
    atomic_write_json(proof_path, proof)
    return proof


def run_inference(
    plan: dict[str, Any],
    recipe: dict[str, Any],
    endpoint: str,
    payload_path: Path,
    output_path: Path,
    response_path: Path,
    proof_path: Path,
    *,
    timeout: float = 1800,
    poll_interval: float = 2,
    opener: OpenUrl = urllib.request.urlopen,
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
                for path in (
                    generated_request_path(proof_path),
                    response_path,
                    output_path,
                    proof_path,
                )
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
            artifacts=[
                generated_request_path(proof_path),
                response_path,
                output_path,
                proof_path,
            ],
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
        expected_recipe = (ROOT / plan["verification"]["recipe_ref"]).resolve()
        model_root = (ROOT / "models").resolve()
        if (
            not expected_recipe.is_relative_to(model_root)
            or args.recipe.resolve() != expected_recipe
        ):
            raise HarnessError("--recipe 与已审核计划的 recipe_ref 不一致")
        run_inference(
            plan,
            load_document(args.recipe),
            args.endpoint,
            args.request_payload,
            args.media_output,
            args.response_output,
            args.proof_output,
            archive=DeploymentArchive(plan["deployment_id"]),
        )
        print(f"COMPLETED: {args.proof_output}")
        return 0
    except (HarnessError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
