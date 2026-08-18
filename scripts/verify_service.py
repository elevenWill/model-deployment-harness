from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from contextlib import suppress
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
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
    from scripts.run_inference import resolve_output_binding
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from _common import (  # type: ignore[no-redef]
        ROOT,
        HarnessError,
        atomic_write_json,
        canonical_plan_sha256,
        file_sha256,
        load_document,
        validate_instance,
    )
    from deployment_archive import DeploymentArchive  # type: ignore[no-redef]
    from run_inference import resolve_output_binding  # type: ignore[no-redef]

LEVELS = (
    "L1_environment",
    "L2_process",
    "L3_port",
    "L4_api",
    "L5_real_inference",
    "L6_output_validation",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_overall(
    levels: dict[str, dict[str, Any]], generation_duration_seconds: float | None,
    artifacts_verified: bool,
) -> str:
    if levels["L5_real_inference"]["status"] != "PASS":
        return "FAILED" if levels["L5_real_inference"]["status"] == "FAIL" else "INCOMPLETE"
    if levels["L6_output_validation"]["status"] != "PASS":
        return "FAILED" if levels["L6_output_validation"]["status"] == "FAIL" else "INCOMPLETE"
    if generation_duration_seconds is None or not artifacts_verified:
        return "INCOMPLETE"
    return "VERIFIED"


def validate_media(path: Path, contract: dict[str, Any]) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "输出文件缺失或为空"
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        return False, "技术媒体校验需要 ffprobe 和 ffmpeg"
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if decode.returncode != 0:
        return False, f"完整解码失败：{decode.stderr[-500:]}"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return False, f"ffprobe 失败：{probe.stderr[-500:]}"
    metadata = json.loads(probe.stdout)
    streams = metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    expected = contract["output_validation"]
    if not video or video.get("codec_name") != expected["video"]["codec"]:
        return False, "缺少所需的视频流或编解码器"
    if not audio or audio.get("codec_name") != expected["audio"]["codec"]:
        return False, "缺少所需的音频流或编解码器"
    if int(audio.get("sample_rate", 0)) != int(expected["audio"]["sample_rate_hz"]):
        return False, "音频采样率与配方不一致"
    if int(audio.get("channels", 0)) != int(expected["audio"]["channels"]):
        return False, "音频声道数与配方不一致"
    try:
        fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
    except (ValueError, ZeroDivisionError):
        fps = 0
    if abs(fps - float(expected["video"]["fps"])) > 0.05:
        return False, "视频帧率与配方不一致"
    duration = float(metadata.get("format", {}).get("duration", 0))
    duration_rule = expected["duration_seconds"]
    if not duration_rule["minimum"] <= duration <= duration_rule["maximum"]:
        return False, "媒体时长超出配方范围"
    return True, "容器、完整解码、流、编解码器、采样率和时长均已通过"


def _verified_artifact(reference: dict[str, Any]) -> bool:
    path = Path(reference["path"])
    return path.is_file() and file_sha256(path).lower() == reference["sha256"].lower()


def _artifact_for(path: Path, media_type: str) -> dict[str, str]:
    return {"path": str(path), "sha256": file_sha256(path), "media_type": media_type}


def _validate_inference_proof(
    proof: dict[str, Any], plan: dict[str, Any], output_path: Path,
    recipe: dict[str, Any] | None = None,
) -> float:
    validate_instance(proof, "inference-proof.schema.json")
    if proof["deployment_id"] != plan["deployment_id"]:
        raise HarnessError("推理证明的 deployment_id 与计划不一致")
    if proof["plan_sha256"].lower() != plan["review"]["plan_sha256"].lower():
        raise HarnessError("推理证明未绑定至已审核计划哈希")

    endpoint = urlparse(proof["endpoint"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise HarnessError("推理端点必须为 HTTP(S) URI")
    if endpoint.port != plan["service"]["port"]:
        raise HarnessError("推理端点端口与已审核服务不一致")
    bind_host = plan["service"]["bind_host"]
    permitted_hosts = {bind_host}
    if bind_host in {"0.0.0.0", "::"}:
        permitted_hosts.update({"127.0.0.1", "localhost", "::1"})
    if endpoint.hostname not in permitted_hosts:
        raise HarnessError("推理端点主机与已审核服务不一致")
    request_path = proof["request"]["path"]
    if endpoint.path not in {"", "/"} or request_path.startswith("//"):
        raise HarnessError("推理证明中的端点或请求路径格式错误")
    allowed_paths = (recipe or {}).get("inference_api", {}).get("submit_paths", [])
    if allowed_paths and request_path not in allowed_paths:
        raise HarnessError("验证配方不允许该推理请求路径")

    for name, artifact in (
        ("请求负载", proof["request"]["payload"]),
        ("作业响应", proof["job"]["response"]),
        ("生成输出", proof["output"]),
    ):
        if not _verified_artifact(artifact):
            raise HarnessError(f"推理证明的{name}缺失或 SHA-256 不正确")
    payload_ref = proof["request"]["payload"]
    response_ref = proof["job"]["response"]
    if payload_ref["media_type"] != "application/json" or response_ref["media_type"] != (
        "application/json"
    ):
        raise HarnessError("推理请求和响应证据必须为 JSON")
    try:
        payload = json.loads(Path(payload_ref["path"]).read_text(encoding="utf-8"))
        response = json.loads(Path(response_ref["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("推理请求/响应证据不是有效 JSON") from exc
    if not isinstance(payload, dict) or not payload:
        raise HarnessError("推理请求载荷必须为非空 JSON 对象")
    if not isinstance(response, dict) or str(response.get("id")) != proof["job"]["job_id"]:
        raise HarnessError("任务响应不包含已记录的任务 ID")
    if str(response.get("status", "")).upper() != "COMPLETED":
        raise HarnessError("任务响应未记录已完成的推理")
    if recipe is None:
        raise HarnessError("推理证明校验必须提供已审核验证配方")
    reconstructed_binding = resolve_output_binding(
        response,
        proof["job"]["job_id"],
        recipe.get("inference_api", {}),
        proof["endpoint"].rstrip("/") + "/",
        payload,
    )
    if any(
        proof["output_binding"].get(key) != value
        for key, value in reconstructed_binding.items()
    ):
        raise HarnessError("推理证明中的输出绑定与原始完成响应不一致")
    if Path(proof["output"]["path"]).resolve() != output_path.resolve():
        raise HarnessError("推理证明输出路径与 --media 不一致")
    if proof["output"]["sha256"].lower() != file_sha256(output_path).lower():
        raise HarnessError("推理证明输出哈希与 --media 不一致")
    response_hash = reconstructed_binding.get("response_content_sha256")
    if response_hash is not None and response_hash != proof["output"]["sha256"].lower():
        raise HarnessError("生成输出哈希与原始完成响应声明的产物不一致")
    binding = proof["output_binding"]
    actual_hash = file_sha256(output_path).lower()
    if binding["download_sha256"].lower() != actual_hash:
        raise HarnessError("下载时计算的输出哈希与当前输出不一致")
    if int(binding["download_content_length"]) != output_path.stat().st_size:
        raise HarnessError("下载时记录的内容长度与当前输出不一致")
    header_length = binding["response_headers"].get("content_length")
    if header_length is not None and int(header_length) != output_path.stat().st_size:
        raise HarnessError("输出响应 Content-Length 与当前输出不一致")
    submitted = datetime.fromisoformat(proof["request"]["submitted_at"].replace("Z", "+00:00"))
    completed = datetime.fromisoformat(proof["job"]["completed_at"].replace("Z", "+00:00"))
    downloaded = datetime.fromisoformat(binding["downloaded_at"].replace("Z", "+00:00"))
    duration = (completed - submitted).total_seconds()
    if duration < 0 or downloaded < completed:
        raise HarnessError("推理提交、任务完成和输出下载时间顺序无效")
    if binding["resolver"] == "RECIPE_OUTPUT_ITEM":
        token_rule = recipe["inference_api"]["output_reference"]["job_token"]
        max_delay = token_rule.get("max_download_delay_seconds")
        if not isinstance(max_delay, (int, float)) or max_delay <= 0:
            raise HarnessError("ComfyUI 配方缺少有效的立即下载时间上限")
        if (downloaded - completed).total_seconds() > float(max_delay):
            raise HarnessError("ComfyUI 输出未在任务完成后按配方要求立即下载")
    return duration


def _validate_semantic_review(
    review: dict[str, Any], plan: dict[str, Any], output_path: Path
) -> None:
    validate_instance(review, "semantic-review.schema.json")
    if review["deployment_id"] != plan["deployment_id"]:
        raise HarnessError("语义审核的 deployment_id 与计划不一致")
    if review["plan_sha256"].lower() != plan["review"]["plan_sha256"].lower():
        raise HarnessError("语义审核未绑定至已审核计划哈希")
    if review["output_sha256"].lower() != file_sha256(output_path).lower():
        raise HarnessError("语义审核未绑定至生成输出")


def build_result(
    observations: dict[str, Any], plan: dict[str, Any], output_path: Path | None = None,
    recipe: dict[str, Any] | None = None, inference_proof_path: Path | None = None,
    semantic_review_path: Path | None = None,
) -> dict[str, Any]:
    if recipe is None:
        raise HarnessError("必须提供验证配方")
    if recipe.get("model_id") != plan["model"]["id"]:
        raise HarnessError("验证配方 model_id 与计划不一致")
    validate_instance(plan, "deployment-plan.schema.json")
    if plan["review"]["plan_sha256"] != canonical_plan_sha256(plan):
        raise HarnessError("验证计划哈希与已审核计划不一致")
    for field in ("deployment_id",):
        if observations.get(field) != plan.get(field):
            raise HarnessError(f"observations.{field} 与计划不一致")
    if observations.get("host_id") != plan["target"]["host_id"]:
        raise HarnessError("observations.host_id 与计划不一致")
    if observations.get("framework") != {
        "name": plan["framework"]["name"],
        "version": plan["framework"]["version"],
    }:
        raise HarnessError("观测到的框架与计划不一致")
    levels = observations.get("levels")
    if not isinstance(levels, dict) or set(levels) != set(LEVELS):
        raise HarnessError("observations.levels 必须恰好包含 L1 至 L6")
    artifacts: list[dict[str, Any]] = list(observations.get("artifacts", []))
    inference_duration: float | None = None
    if any(not _verified_artifact(item) for item in artifacts):
        raise HarnessError("提供的验证制品缺失或 SHA-256 不正确")
    if output_path is not None:
        if inference_proof_path is None:
            levels["L5_real_inference"] = {
                "status": "BLOCKED", "checked_at": utc_now(),
                "detail": "未提供带类型的推理证明", "evidence": [],
            }
        else:
            proof = load_document(inference_proof_path)
            inference_duration = _validate_inference_proof(proof, plan, output_path, recipe)
            proof_artifact = _artifact_for(inference_proof_path, "application/json")
            artifacts.append(proof_artifact)
            levels["L5_real_inference"] = {
                "status": "PASS", "checked_at": utc_now(),
                "detail": (
                    "请求、已完成任务响应和输出已绑定至已审核端点和计划"
                ),
                "evidence": [
                    proof_artifact, proof["request"]["payload"],
                    proof["job"]["response"], proof["output"],
                ],
            }
        technical_pass, detail = validate_media(output_path, recipe)
        if technical_pass and semantic_review_path is None:
            levels["L6_output_validation"] = {
                "status": "BLOCKED",
                "checked_at": utc_now(),
                "detail": detail + "；未提供带类型的语义审核",
                "evidence": [],
            }
        else:
            if technical_pass:
                semantic_review = load_document(semantic_review_path)
                _validate_semantic_review(semantic_review, plan, output_path)
                semantic_artifact = _artifact_for(semantic_review_path, "application/json")
                artifacts.append(semantic_artifact)
            levels["L6_output_validation"] = {
                "status": "PASS" if technical_pass else "FAIL",
                "checked_at": utc_now(),
                "detail": detail,
            }
        if output_path.is_file():
            artifact = {
                "path": str(output_path),
                "sha256": file_sha256(output_path),
                "media_type": "video/mp4",
            }
            artifacts.append(artifact)
            levels["L6_output_validation"]["evidence"] = [artifact]
            if technical_pass and semantic_review_path is not None:
                levels["L6_output_validation"]["evidence"].append(semantic_artifact)
    elif recipe is not None and "output_validation" in recipe:
        levels["L6_output_validation"] = {
            "status": "BLOCKED",
            "checked_at": utc_now(),
            "detail": "配方要求媒体制品；未提供 --media",
            "evidence": [],
        }
    generation_duration = inference_duration
    result = {
        "schema_version": "1.0",
        "verification_id": observations["verification_id"],
        "deployment_id": observations["deployment_id"],
        "host_id": observations["host_id"],
        "plan_sha256": plan["review"]["plan_sha256"],
        "recipe_ref": plan["verification"]["recipe_ref"],
        "started_at": observations["started_at"],
        "completed_at": observations.get("completed_at", utc_now()),
        "levels": levels,
        "overall_status": evaluate_overall(
            levels,
            generation_duration,
            bool(artifacts) and all(_verified_artifact(item) for item in artifacts),
        ),
        "metrics": {
            "generation_duration_seconds": generation_duration,
            "peak_vram_bytes": observations.get("metrics", {}).get("peak_vram_bytes"),
            "peak_ram_bytes": observations.get("metrics", {}).get("peak_ram_bytes"),
        },
        "framework": observations["framework"],
        "gpu_topology_summary": observations.get("gpu_topology_summary", ""),
        "command_redacted": observations.get("command_redacted", []),
        "artifacts": artifacts,
    }
    validate_instance(result, "verification-result.schema.json")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建并校验 L1-L6 验证结果")
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--media", type=Path)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--inference-proof", type=Path)
    parser.add_argument("--semantic-review", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive: DeploymentArchive | None = None
    plan: dict[str, Any] | None = None
    try:
        plan = load_document(args.plan)
        validate_instance(plan, "deployment-plan.schema.json")
        archive = DeploymentArchive(plan["deployment_id"])
        expected_recipe = (ROOT / plan["verification"]["recipe_ref"]).resolve()
        if not expected_recipe.is_relative_to((ROOT / "models").resolve()):
            raise HarnessError("已审核验证配方越出了 models 目录")
        if args.recipe.resolve() != expected_recipe:
            raise HarnessError("--recipe 与已审核计划的 recipe_ref 不一致")
        result = build_result(
            load_document(args.observations),
            plan,
            args.media,
            load_document(args.recipe) if args.recipe else None,
            args.inference_proof,
            args.semantic_review,
        )
        atomic_write_json(args.output, result)
        artifact_paths = [
            args.plan,
            args.observations,
            args.recipe,
            args.output,
            *(Path(item["path"]) for item in result["artifacts"]),
        ]
        archive.record(
            stage="VERIFY",
            status=(
                "VERIFIED"
                if result["overall_status"] == "VERIFIED"
                else "FAILED"
                if result["overall_status"] == "FAILED"
                else "INCOMPLETE"
            ),
            summary=(
                "L1 至 L6 验证通过，部署已确认可用"
                if result["overall_status"] == "VERIFIED"
                else "验证未完全通过，已保留证据供排查"
            ),
            host_id=str(plan["target"]["host_id"]),
            artifacts=list(dict.fromkeys(artifact_paths)),
            details={
                "verification_ref": str(args.output),
                "workload": {"recipe_ref": plan["verification"]["recipe_ref"]},
                "environment": {
                    "framework": plan["framework"]["name"],
                    "framework_version": plan["framework"]["version"],
                    "gpu_topology": result["gpu_topology_summary"] or "未记录",
                },
                "metrics": result["metrics"],
                "deployment": {
                    "request_ref": f"request:{plan['request_id']}",
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
                },
            },
        )
        print(f"{result['overall_status']}: {args.output}")
        return 0 if result["overall_status"] == "VERIFIED" else 3
    except (HarnessError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if archive is not None and plan is not None:
            target = plan.get("target")
            host_id = (
                str(target["host_id"])
                if isinstance(target, dict) and target.get("host_id")
                else None
            )
            artifacts = tuple(
                path
                for path in (
                    args.plan,
                    args.observations,
                    args.recipe,
                    args.media,
                    args.inference_proof,
                    args.semantic_review,
                    args.output,
                )
                if path is not None and path.is_file()
            )
            with suppress(HarnessError, OSError):
                archive.record(
                    stage="VERIFY",
                    status="BLOCKED",
                    summary="验证未能完成，已保留现有证据",
                    host_id=host_id,
                    artifacts=artifacts,
                    details={"error_type": exc.__class__.__name__},
                )
        print(f"BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
