from __future__ import annotations

import json
from copy import deepcopy

import pytest

from scripts._common import ROOT, canonical_plan_sha256, file_sha256
from scripts.deployment_archive import DeploymentArchive
from scripts.probe_host import CommandResult
from scripts.remote_exec import (
    ExecutionBlocked,
    archive_reviewed_lifecycle,
    authorize_execution,
    execute_plan,
    validate_executable_plan,
)


def ready_plan():
    step = {
        "step_id": "mkdir-install",
        "sequence": 1,
        "name": "create root",
        "action": "create_target_directory",
        "action_class": "PLAN_ALLOWED_WRITE",
        "command": ["mkdir", "-p", "/opt/models/example"],
        "success_criteria": ["directory exists"],
        "rollback_step_ids": ["inspect-rollback"],
    }
    rollback = {
        "step_id": "inspect-rollback",
        "sequence": 1,
        "name": "inspect",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["test", "-d", "/opt/models/example"],
        "success_criteria": ["state observed"],
        "rollback_step_ids": [],
    }
    lifecycle_files = {
        "INTAKE": "tests/fixtures/lifecycle/intake.json",
        "REQUIREMENT_GATE": "tests/fixtures/lifecycle/requirement-gate.json",
        "HOST_DISCOVERY": "tests/fixtures/lifecycle/host-discovery.json",
        "RESEARCH": "tests/fixtures/lifecycle/research.json",
        "PLAN": "tests/fixtures/lifecycle/plan.json",
        "PLAN_REVIEW": "tests/fixtures/lifecycle/plan-review.json",
    }
    plan = {
        "schema_version": "1.0",
        "deployment_id": "dep-1",
        "request_id": "req-1",
        "created_at": "2026-08-17T00:00:00Z",
        "host_profile_observed_at": "2026-08-17T00:01:00Z",
        "target": {
            "host_id": "knode25",
            "gpu_ids": [0],
            "install_root": "/opt/models",
            "model_root": "/models",
        },
        "model": {
            "id": "minimax-h3",
            "variant": "fl2va",
            "recipe_ref": "models/minimax-h3/manifest.yaml",
        },
        "framework": {
            "name": "sglang",
            "version": "a54de989c8ba817ebb603c5443e694e5fcf7edb1",
            "recipe_ref": "models/minimax-h3/recipes/sglang.yaml",
            "runtime_artifact": {
                "kind": "source_checkout",
                "location": "/opt/models/sglang",
                "revision": "a54de989c8ba817ebb603c5443e694e5fcf7edb1",
                "probe_command": [
                    "git",
                    "-C",
                    "/opt/models/sglang",
                    "rev-parse",
                    "HEAD",
                ],
                "executable": "/opt/models/sglang/.venv/bin/sglang",
            },
            "rationale": "official support",
            "evidence_ids": ["ev-1"],
        },
        "compatibility": {"profile_id": "fixture-compatible", "required_cuda": "12.6"},
        "environment": {"strategy": "container", "isolated": True, "rationale": "preserve host"},
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "lifecycle": {
            "transitions": [
                {
                    "stage": stage,
                    "completed_at": "2026-08-17T00:00:00Z",
                    "artifact": {
                        "path": lifecycle_files[stage],
                        "sha256": file_sha256(ROOT / lifecycle_files[stage]),
                    },
                }
                for stage in (
                    "INTAKE",
                    "REQUIREMENT_GATE",
                    "HOST_DISCOVERY",
                    "RESEARCH",
                    "PLAN",
                    "PLAN_REVIEW",
                )
            ]
        },
        "executor_controls": {
            "remote_writer_lock": {
                "path": "/tmp/model-deployment-harness-writer-lock",
                "acquire_command": ["mkdir", "/tmp/model-deployment-harness-writer-lock"],
                "release_command": ["rmdir", "/tmp/model-deployment-harness-writer-lock"],
            }
        },
        "steps": [step],
        "risks": [],
        "required_changes": [
            {
                "description": "create isolated root",
                "action_class": "PLAN_ALLOWED_WRITE",
                "step_ids": ["mkdir-install"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "ev-1",
                "source": {
                    "title": "Official docs",
                    "url": "https://example.com/docs",
                    "authority_tier": "A",
                    "publisher": "Framework",
                },
                "retrieved_at": "2026-08-17T00:00:00Z",
                "claim": "supported",
                "applies_to": ["example"],
                "confidence": "HIGH",
                "officially_verified": True,
                "inference": False,
            }
        ],
        "verification": {
            "recipe_ref": "models/minimax-h3/verify.yaml",
            "required_levels": ["L5_real_inference", "L6_output_validation"],
            "success_condition": "L5_AND_L6_PASS",
        },
        "rollback": {
            "trigger_conditions": ["step failure"],
            "steps": [rollback],
            "preserve_evidence": True,
        },
        "license_gate": {
            "status": "PASS",
            "license_type": "MiniMax H3 Community License",
            "source_url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE",
            "checked_at": "2026-08-17T00:00:00Z",
            "deployment_region": "CN",
            "intended_use": "research",
            "findings": ["allowed"],
            "reviewed_by": "license-reviewer",
            "accepted_by": "operator",
            "accepted_at": "2026-08-17T00:01:00Z",
        },
        "review": {
            "status": "APPROVED",
            "reviewed_by": "reviewer",
            "reviewed_at": "2026-08-17T00:02:00Z",
            "plan_sha256": "0" * 64,
        },
        "status": "READY",
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    return plan


def deployment_request():
    return {
        "schema_version": "1.0",
        "request_id": "req-1",
        "requested_at": "2026-08-17T00:00:00Z",
        "requested_by": "operator",
        "target": {
            "host": {"host_id": "knode25", "ssh_username": "deploy", "ssh_port": 22},
            "gpu_ids": [0],
            "install_root": "/opt/models",
            "model_root": "/models",
        },
        "model": {"id": "minimax-h3", "variant": "fl2va"},
        "framework_preference": "sglang",
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "research",
        "deployment_region": "CN",
    }


def observed_host():
    return {
        "schema_version": "1.0",
        "host_id": "knode25",
        "observed_at": "2026-08-17T00:01:00Z",
        "probe": {"status": "COMPLETE", "transport": "LOCAL_FIXTURE", "errors": []},
        "identity": {"hostname": "knode25", "addresses": ["10.0.0.25"], "aliases": []},
        "hardware": {
            "cpu": {"model": "fixture", "logical_cores": 32, "architecture": "x86_64"},
            "memory_bytes": 512 * 1024**3,
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-fixture",
                    "model": "fixture GPU",
                    "memory_total_bytes": 80 * 1024**3,
                    "memory_free_bytes": 79 * 1024**3,
                }
            ],
            "gpu_topology": [],
        },
        "software": {
            "os": {"name": "Fixture Linux", "version": "1"},
            "kernel": "6.0",
            "nvidia": {"driver_version": "999", "cuda_compatibility": "12.6"},
            "docker": {"installed": True, "version": "fixture", "nvidia_runtime_available": True},
            "python": [{"executable": "python3", "version": "Python 3.11"}],
            "uv_version": None,
            "conda_version": None,
        },
        "storage": {
            "filesystems": [
                {
                    "path": "/",
                    "total_bytes": 1024**4,
                    "available_bytes": 800 * 1024**3,
                    "filesystem_type": "ext4",
                }
            ],
            "mounts": [{"source": "/dev/x", "target": "/", "options": ["rw"]}],
        },
        "network": {"listening_ports": [22], "connectivity_checks": []},
        "runtime": {"gpu_processes": [], "model_services": []},
    }


def ready_comfyui_plan():
    plan = ready_plan()
    install_root = "/opt/h3"
    runtime_root = f"{install_root}/ComfyUI"
    model_root = f"{install_root}/model"
    venv_python = f"{install_root}/.venv/bin/python"
    bootstrap_uv = "/opt/toolchain/bin/uv"
    bootstrap_python = "/opt/toolchain/python/bin/python3.11"
    revision = "0d80858061b511bd38c8cef4c235ef8e01040822"
    plan["target"] = {
        "host_id": "knode25",
        "gpu_ids": [0],
        "install_root": install_root,
        "model_root": model_root,
    }
    plan["model"]["variant"] = "both"
    plan["framework"] = {
        "name": "comfyui",
        "version": revision,
        "recipe_ref": "models/minimax-h3/recipes/comfyui.yaml",
        "runtime_artifact": {
            "kind": "source_checkout",
            "location": runtime_root,
            "revision": revision,
            "probe_command": [
                "git",
                "-C",
                runtime_root,
                "rev-parse",
                "HEAD",
            ],
            "executable": venv_python,
        },
        "rationale": "isolated experimental ComfyUI service",
        "evidence_ids": ["ev-1"],
    }
    plan["compatibility"] = {
        "profile_id": "comfyui-1xrtx3090-int8-convrot-experimental",
        "required_cuda": "12.6",
    }
    plan["environment"] = {
        "strategy": "venv",
        "isolated": True,
        "rationale": "preserve host",
        "bootstrap_uv": bootstrap_uv,
        "bootstrap_python": bootstrap_python,
    }
    plan["service"] = {"mode": "managed_service", "bind_host": "0.0.0.0", "port": 8188}
    model_files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    dependency_argv = [
        bootstrap_uv,
        "pip",
        "install",
        "--python",
        venv_python,
        "--index-url",
        "https://download.pytorch.org/whl/cu121",
        "--extra-index-url",
        "https://pypi.org/simple",
        "torch==2.5.1",
        "torchvision==0.20.1",
        "torchaudio==2.5.1",
        "https://files.pythonhosted.org/packages/27/d1/e53410260b81610233cb56c2fac1a9f3d39887be3cbb983cd8baa6a07528/comfy_kitchen-0.2.31-py3-none-any.whl#sha256=5117946c30f308cfc73b9c26f723ae3918308bd090e57a8eae298406934aabd6",
        "-r",
        f"{runtime_root}/requirements.txt",
        "setuptools==80.9.0",
        "modelscope==1.31.0",
    ]
    commands = [
        ("bootstrap-uv", "inspect", ["test", "-x", bootstrap_uv]),
        ("bootstrap-python", "inspect", ["test", "-x", bootstrap_python]),
        ("systemd-ready", "inspect", ["systemctl", "--user", "is-system-running"]),
        (
            "clone",
            "clone_source_checkout",
            [
                "git",
                "clone",
                "--no-checkout",
                "https://github.com/Comfy-Org/ComfyUI.git",
                runtime_root,
            ],
        ),
        (
            "checkout",
            "checkout_source_revision",
            ["git", "-C", runtime_root, "checkout", "--detach", revision],
        ),
        (
            "venv",
            "create_isolated_venv",
            [bootstrap_uv, "venv", "--python", bootstrap_python, f"{install_root}/.venv"],
        ),
        (
            "deps",
            "install_isolated_dependencies",
            dependency_argv,
        ),
        (
            "download",
            "download_model",
            [
                f"{install_root}/.venv/bin/modelscope",
                "download",
                "--model",
                "Comfy-Org/MiniMax-H3",
                "--revision",
                "a" * 40,
                "--local_dir",
                model_root,
                *model_files,
            ],
        ),
        (
            "models-link",
            "create_service_config",
            ["ln", "-s", model_root, f"{install_root}/models"],
        ),
        (
            "start",
            "start_own_service",
            [
                "systemd-run",
                "--user",
                "--unit=dep-1",
                "--collect",
                f"--property=WorkingDirectory={runtime_root}",
                "--setenv=CUDA_VISIBLE_DEVICES=0",
                venv_python,
                f"{runtime_root}/main.py",
                "--listen",
                "0.0.0.0",
                "--port",
                "8188",
                "--base-directory",
                install_root,
                "--lowvram",
                "--disable-auto-launch",
            ],
        ),
    ]
    plan["steps"] = [
        {
            "step_id": step_id,
            "sequence": index,
            "name": step_id,
            "action": action,
            "action_class": "READ_ONLY" if action == "inspect" else "PLAN_ALLOWED_WRITE",
            "command": command,
            "depends_on": [] if index == 1 else [commands[index - 2][0]],
            "success_criteria": ["fixture"],
            "rollback_step_ids": [],
        }
        for index, (step_id, action, command) in enumerate(commands, start=1)
    ]
    plan["steps"][-1]["depends_on"].append("systemd-ready")
    venv_step = next(step for step in plan["steps"] if step["step_id"] == "venv")
    venv_step["depends_on"] = ["checkout", "bootstrap-uv", "bootstrap-python"]
    plan["required_changes"] = [
        {
            "description": step["name"],
            "action_class": "PLAN_ALLOWED_WRITE",
            "step_ids": [step["step_id"]],
        }
        for step in plan["steps"]
    ]
    plan["verification"]["recipe_ref"] = "models/minimax-h3/verify-comfyui.yaml"
    plan["rollback"] = {
        "trigger_conditions": ["start or verification failure"],
        "steps": [
            {
                "step_id": "stop-comfyui",
                "sequence": 1,
                "name": "stop owned unit",
                "action": "stop_own_service",
                "action_class": "PLAN_ALLOWED_WRITE",
                "command": ["systemctl", "--user", "stop", "dep-1.service"],
                "success_criteria": ["owned unit stopped"],
                "rollback_step_ids": [],
            }
        ],
        "preserve_evidence": True,
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    return plan


class FakeTransport:
    def __init__(self, returncode=0, raised=None):
        self.calls = []
        self.uploads = []
        self.returncode = returncode
        self.raised = raised

    def run(self, argv, *, timeout=20, cwd=None, env=None):
        self.calls.append((tuple(argv), cwd, dict(env or {})))
        if self.raised:
            raise self.raised
        if tuple(argv) == (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ):
            return CommandResult(tuple(argv), 0, "a54de989c8ba817ebb603c5443e694e5fcf7edb1\n", "")
        return CommandResult(tuple(argv), self.returncode, "ok", "")

    def upload_new(self, source, destination, *, timeout=300):
        self.uploads.append((source, destination, timeout))

    def close(self):
        pass


def test_staged_source_bundle_requires_exclusive_transfer_and_hashes(tmp_path):
    plan = ready_comfyui_plan()
    bundle = tmp_path / "comfyui.bundle"
    bundle.write_bytes(b"verified source bundle")
    bundle_sha = file_sha256(bundle)
    tree = "e2791b95dc97f50ef97a22499131160b605edd47"
    runtime = plan["framework"]["runtime_artifact"]
    runtime["source_bundle"] = {
        "local_path": str(bundle),
        "remote_path": "/opt/h3/.comfyui.bundle",
        "sha256": bundle_sha,
        "tree": tree,
    }
    replacements = {
        "clone": (
            "clone-source",
            "clone_source_checkout",
            ["git", "clone", "--no-checkout", "/opt/h3/.comfyui.bundle", "/opt/h3/ComfyUI"],
        ),
        "checkout": (
            "checkout-source",
            "checkout_source_revision",
            ["git", "-C", "/opt/h3/ComfyUI", "checkout", "--detach", runtime["revision"]],
        ),
    }
    rebuilt = []
    for step in plan["steps"]:
        replacement = replacements.get(step["step_id"])
        if replacement is None:
            rebuilt.append(step)
            continue
        step_id, action, command = replacement
        step["step_id"], step["action"], step["command"] = step_id, action, command
        rebuilt.append(step)
    rebuilt[3:3] = [
        {
            "step_id": "source-bundle-absent",
            "sequence": 0,
            "name": "source bundle absent",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["test", "!", "-e", "/opt/h3/.comfyui.bundle"],
            "depends_on": ["systemd-ready"],
            "success_criteria": ["absent"],
            "rollback_step_ids": [],
        },
        {
            "step_id": "stage-source-bundle",
            "sequence": 0,
            "name": "stage source bundle",
            "action": "stage_source_bundle",
            "action_class": "PLAN_ALLOWED_WRITE",
            "command": ["sftp-upload", str(bundle), "/opt/h3/.comfyui.bundle"],
            "depends_on": ["source-bundle-absent"],
            "success_criteria": ["exclusive upload"],
            "rollback_step_ids": [],
        },
        {
            "step_id": "verify-source-bundle",
            "sequence": 0,
            "name": "verify source bundle",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["sha256sum", "/opt/h3/.comfyui.bundle"],
            "depends_on": ["stage-source-bundle"],
            "success_criteria": ["sha matches"],
            "rollback_step_ids": [],
        },
    ]
    checkout_index = next(
        index for index, step in enumerate(rebuilt) if step["step_id"] == "checkout-source"
    )
    rebuilt.insert(
        checkout_index + 1,
        {
            "step_id": "verify-source-tree",
            "sequence": 0,
            "name": "verify source tree",
            "action": "inspect",
            "action_class": "READ_ONLY",
            "command": ["git", "-C", "/opt/h3/ComfyUI", "rev-parse", "HEAD^{tree}"],
            "depends_on": ["checkout-source"],
            "success_criteria": ["tree matches"],
            "rollback_step_ids": [],
        },
    )
    for index, step in enumerate(rebuilt, start=1):
        step["sequence"] = index
    clone = next(step for step in rebuilt if step["step_id"] == "clone-source")
    clone["depends_on"] = ["verify-source-bundle"]
    for step in rebuilt:
        step["depends_on"] = [
            {"clone": "clone-source", "checkout": "checkout-source"}.get(item, item)
            for item in step.get("depends_on", [])
        ]
    plan["steps"] = rebuilt
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)

    class BundleTransport(FakeTransport):
        def run(self, argv, **kwargs):
            if tuple(argv) == ("sha256sum", "/opt/h3/.comfyui.bundle"):
                return CommandResult(tuple(argv), 0, f"{bundle_sha}  .comfyui.bundle\n", "")
            if tuple(argv) == ("git", "-C", "/opt/h3/ComfyUI", "rev-parse", "HEAD^{tree}"):
                return CommandResult(tuple(argv), 0, f"{tree}\n", "")
            if tuple(argv)[-2:] == ("rev-parse", "HEAD"):
                return CommandResult(tuple(argv), 0, f"{runtime['revision']}\n", "")
            return super().run(argv, **kwargs)

    transport = BundleTransport()
    result = execute_plan(
        plan,
        transport,
        request=deployment_request()
        | {
            "framework_preference": "comfyui",
            "target": deployment_request()["target"]
            | {"install_root": "/opt/h3", "model_root": "/opt/h3/model"},
            "model": {"id": "minimax-h3", "variant": "both"},
            "service": plan["service"],
        },
        host_profile=observed_host(),
        _probe_collector=lambda _transport: observed_host(),
    )
    assert result.status == "EXECUTED"
    assert transport.uploads == [(bundle, "/opt/h3/.comfyui.bundle", 300)]


def test_ready_hash_validated_and_exact_argv_executed(tmp_path):
    plan = ready_plan()
    transport = FakeTransport()
    result = execute_plan(
        plan,
        transport,
        request=deployment_request(),
        host_profile=observed_host(),
        lock_directory=tmp_path,
        _probe_collector=lambda _: observed_host(),
    )
    assert result.status == "EXECUTED"
    assert [call[0] for call in transport.calls] == [
        (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ),
        ("mkdir", "/tmp/model-deployment-harness-writer-lock"),
        (
            "git",
            "-C",
            "/opt/models/sglang",
            "rev-parse",
            "HEAD",
        ),
        tuple(plan["steps"][0]["command"]),
        ("rmdir", "/tmp/model-deployment-harness-writer-lock"),
    ]


def test_comfyui_plan_only_allows_exact_modelscope_assets_and_isolated_service() -> None:
    plan = ready_comfyui_plan()
    validate_executable_plan(plan)
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    download["command"][-1] = "diffusion_models/minimax_h3_fl2va_bf16.safetensors"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="ModelScope"):
        validate_executable_plan(plan)


def test_comfyui_master_download_requires_postdownload_integrity_check() -> None:
    plan = ready_comfyui_plan()
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    revision = download["command"].index("--revision")
    download["command"][revision + 1] = "master"
    files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 90,
        "name": "verify ModelScope asset integrity",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": ["download"],
        "success_criteria": ["all SHA-256 values match the observed master manifest"],
        "rollback_step_ids": [],
    }
    link = next(step for step in plan["steps"] if step["action"] == "create_service_config")
    link["depends_on"].append("verify-model-assets")
    plan["steps"].append(checksum)
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)

    checksum["depends_on"] = []
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="可变 ModelScope master"):
        validate_executable_plan(plan)


def test_comfyui_master_allows_serialized_position_file_downloads() -> None:
    plan = ready_comfyui_plan()
    download = next(step for step in plan["steps"] if step["action"] == "download_model")
    revision = download["command"].index("--revision")
    download["command"][revision + 1] = "master"
    files = download["command"][8:]
    download["command"] = download["command"][:8] + [files[0]]
    download["step_id"] = "download-1"
    download["name"] = "download first approved file"
    downloads = [download]
    for index, file in enumerate(files[1:], start=2):
        serial = deepcopy(download)
        serial["step_id"] = f"download-{index}"
        serial["sequence"] = 20 + index
        serial["name"] = f"download approved file {index}"
        serial["command"] = serial["command"][:8] + [file]
        serial["depends_on"] = [downloads[-1]["step_id"]]
        plan["steps"].append(serial)
        downloads.append(serial)
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 90,
        "name": "verify ModelScope asset integrity",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": [item["step_id"] for item in downloads],
        "success_criteria": ["all SHA-256 values match the observed master manifest"],
        "rollback_step_ids": [],
    }
    link = next(step for step in plan["steps"] if step["action"] == "create_service_config")
    link["depends_on"].append("verify-model-assets")
    plan["steps"].append(checksum)
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)


def test_comfyui_recovery_reuses_verified_model_assets_before_restart() -> None:
    plan = ready_comfyui_plan()
    plan["framework"]["runtime_artifact"]["reuse_verified_model_assets"] = True
    plan["steps"] = [
        step
        for step in plan["steps"]
        if step["action"] not in {"download_model", "create_service_config"}
    ]
    files = [
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_fp16.safetensors",
    ]
    checksum = {
        "step_id": "verify-model-assets",
        "sequence": 20,
        "name": "verify reused ModelScope assets",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["sha256sum", *(f"/opt/h3/model/{item}" for item in files)],
        "depends_on": [],
        "success_criteria": ["all reused assets match the official manifest"],
        "rollback_step_ids": [],
    }
    model_link = {
        "step_id": "verify-model-link",
        "sequence": 21,
        "name": "verify reused models link",
        "action": "inspect",
        "action_class": "READ_ONLY",
        "command": ["test", "-e", "/opt/h3/models"],
        "depends_on": ["verify-model-assets"],
        "success_criteria": ["models link remains available"],
        "rollback_step_ids": [],
    }
    start = next(step for step in plan["steps"] if step["action"] == "start_own_service")
    start["depends_on"] = ["systemd-ready", "verify-model-assets", "verify-model-link"]
    plan["steps"].extend([checksum, model_link])
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)


def test_comfyui_execution_blocks_without_reviewed_runtime_paths() -> None:
    plan = ready_comfyui_plan()
    request = deployment_request()
    request["target"]["install_root"] = "/opt/h3"
    request["target"]["model_root"] = "/opt/h3/model"
    request["model"]["variant"] = "both"
    request["framework_preference"] = "comfyui"
    request["service"] = {"mode": "managed_service", "bind_host": "0.0.0.0", "port": 8188}
    del plan["environment"]["bootstrap_uv"]
    with pytest.raises(ExecutionBlocked, match="独立 uv 与 Python 路径"):
        authorize_execution(plan, request, observed_host())


def test_comfyui_uses_reviewed_runtime_when_ssh_path_hides_uv() -> None:
    plan = ready_comfyui_plan()
    request = deployment_request()
    request["target"]["install_root"] = "/opt/h3"
    request["target"]["model_root"] = "/opt/h3/model"
    request["model"]["variant"] = "both"
    request["framework_preference"] = "comfyui"
    request["service"] = {"mode": "managed_service", "bind_host": "0.0.0.0", "port": 8188}
    host = observed_host()
    host["software"]["uv_version"] = "bash: uv: command not found"
    host["software"]["python"] = [{"executable": "python3", "version": "Python 3.8.10"}]
    assert authorize_execution(plan, request, host).status == "PASS"


def test_execute_plan_automatically_records_redacted_step_results(tmp_path):
    plan = ready_plan()
    archive = DeploymentArchive(
        plan["deployment_id"],
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    result = execute_plan(
        plan,
        FakeTransport(),
        request=deployment_request(),
        host_profile=observed_host(),
        lock_directory=tmp_path,
        _probe_collector=lambda _: observed_host(),
        archive=archive,
    )

    assert result.status == "EXECUTED"
    execution = archive.directory / "execution-0001.json"
    assert execution.is_file()
    document = json.loads(execution.read_text(encoding="utf-8"))
    assert document["steps"] == [
        {
            "step_id": "mkdir-install",
            "started_at": document["steps"][0]["started_at"],
            "completed_at": document["steps"][0]["completed_at"],
            "returncode": 0,
            "stdout_redacted": "ok",
            "stderr_redacted": "",
        }
    ]
    assert document["steps"][0]["started_at"] <= document["steps"][0]["completed_at"]
    deployment_record_path = tmp_path / "deployments" / "dep-1.json"
    deployment_record = json.loads(deployment_record_path.read_text(encoding="utf-8"))
    assert deployment_record["deployment_status"] == "STARTED"
    assert deployment_record["known_state"]["expected_service_state"] == "RUNNING"
    assert deployment_record["framework"]["name"] == "sglang"


def test_preflight_exception_is_automatically_recorded_as_failed_deployment(tmp_path):
    plan = ready_plan()
    archive = DeploymentArchive(
        plan["deployment_id"],
        root=tmp_path / "deployments",
        knowledge_root=tmp_path / "knowledge",
    )
    request = deployment_request()
    request["target"]["gpu_ids"] = [1]
    with pytest.raises(ExecutionBlocked, match="制品不匹配"):
        execute_plan(
            plan,
            FakeTransport(),
            request=request,
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
            archive=archive,
        )

    record = json.loads((tmp_path / "deployments" / "dep-1.json").read_text(encoding="utf-8"))
    assert record["deployment_status"] == "FAILED"
    assert record["incident_refs"] == ["incident-dep-1-0001"]


def test_reviewed_plan_archives_all_prior_lifecycle_artifacts(tmp_path):
    plan = ready_plan()
    request_path = tmp_path / "request.json"
    profile_path = tmp_path / "host-profile.json"
    plan_path = tmp_path / "plan.json"
    request_path.write_text(json.dumps(deployment_request()), encoding="utf-8")
    profile_path.write_text(json.dumps(observed_host()), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    archive = DeploymentArchive(plan["deployment_id"], root=tmp_path / "deployments")

    archive_reviewed_lifecycle(
        archive,
        plan,
        request_path=request_path,
        host_profile_path=profile_path,
        plan_path=plan_path,
    )

    document = json.loads(archive.path.read_text(encoding="utf-8"))
    assert [event["stage"] for event in document["events"]] == [
        "INTAKE",
        "REQUIREMENT_GATE",
        "HOST_DISCOVERY",
        "RESEARCH",
        "PLAN",
        "PLAN_REVIEW",
    ]
    assert all(event["status"] == "PASS" for event in document["events"])
    review_paths = {item["path"] for item in document["events"][-1]["artifacts"]}
    assert str(plan_path) in review_paths


def test_hash_detects_any_post_review_change():
    plan = ready_plan()
    plan["steps"][0]["command"].append("changed")
    with pytest.raises(ExecutionBlocked, match="SHA-256"):
        validate_executable_plan(plan)


def test_unknown_action_fails_closed():
    plan = ready_plan()
    plan["steps"][0].update(action="other", action_class="PLAN_ALLOWED_WRITE")
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="未知动作"):
        validate_executable_plan(plan)


def test_protected_action_requires_approval_scoped_to_step_and_action():
    plan = ready_plan()
    plan["steps"][0].update(
        action="change_system_cuda",
        action_class="PROTECTED",
        approval={
            "status": "APPROVED",
            "approved_by": "operator",
            "approved_at": "2026-08-17T00:03:00Z",
            "scope": "change_system_cuda",
        },
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="限定"):
        validate_executable_plan(plan)


def test_even_scoped_protected_action_is_not_automated():
    plan = ready_plan()
    plan["steps"][0].update(
        action="change_system_cuda",
        action_class="PROTECTED",
        approval={
            "status": "APPROVED",
            "approved_by": "operator",
            "approved_at": "2026-08-17T00:03:00Z",
            "scope": "mkdir-install change_system_cuda",
        },
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="绝不自动执行"):
        validate_executable_plan(plan)


def test_artifact_mismatch_and_ssh_failure_stop_before_later_steps(tmp_path):
    plan = ready_plan()
    transport = FakeTransport(raised=ConnectionError("secret details"))
    mismatched = deployment_request()
    mismatched["target"]["gpu_ids"] = [1]
    with pytest.raises(ExecutionBlocked, match="制品不匹配"):
        execute_plan(
            plan,
            transport,
            request=mismatched,
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )
    assert transport.calls == []
    with pytest.raises(ExecutionBlocked, match="框架运行时探测失败") as caught:
        execute_plan(
            plan,
            transport,
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )
    assert "secret details" not in str(caught.value)


def test_secret_value_in_plan_is_rejected_and_output_is_redacted(tmp_path):
    plan = ready_plan()
    plan["steps"][0]["success_criteria"].append("super-secret-token")
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="密钥值"):
        execute_plan(
            plan,
            FakeTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            environ={"HF_TOKEN": "super-secret-token"},
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )


def test_occupied_gpu_cannot_be_bypassed_with_caller_assertion(tmp_path):
    host = observed_host()
    host["runtime"]["gpu_processes"] = [
        {"gpu_id": "GPU-fixture", "pid": 88, "process_name": "unrelated", "memory_bytes": 1}
    ]
    with pytest.raises(ExecutionBlocked, match="占用"):
        execute_plan(
            ready_plan(),
            FakeTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: host,
        )


def test_allowed_action_cannot_smuggle_destructive_argv() -> None:
    plan = ready_plan()
    plan["steps"][0]["command"] = ["rm", "-rf", "/"]
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="与声明动作"):
        validate_executable_plan(plan)


def test_service_label_cannot_smuggle_docker_kill() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=["docker", "kill", "30011"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="精确 run 语法"):
        validate_executable_plan(plan)


def test_container_launch_binds_exact_gpu_ids() -> None:
    plan = ready_plan()
    image = "registry.example/h3@sha256:" + "a" * 64
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=[
            "docker",
            "run",
            "--name",
            "dep-1",
            "--gpus",
            "device=0",
            "--publish",
            "127.0.0.1:30011:30011",
            "--detach",
            image,
        ],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    validate_executable_plan(plan)
    plan["steps"][0]["command"][5] = "all"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="精确 run 语法"):
        validate_executable_plan(plan)


def test_native_service_label_requires_framework_launch_grammar() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="start_own_service",
        action_class="PLAN_ALLOWED_WRITE",
        command=["sglang", "remove", "--port", "30011"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="已审核启动命令"):
        validate_executable_plan(plan)


def test_read_only_label_cannot_smuggle_python_or_docker() -> None:
    plan = ready_plan()
    plan["steps"][0].update(
        action="inspect",
        action_class="READ_ONLY",
        command=["python3", "-c", "import os; os.system('reboot')"],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="与声明动作"):
        validate_executable_plan(plan)


def test_mutable_framework_low_tier_evidence_and_stage_skips_are_blocked() -> None:
    plan = ready_plan()
    plan["framework"]["version"] = "latest"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="不可变"):
        validate_executable_plan(plan)

    plan = ready_plan()
    plan["evidence"][0]["source"]["authority_tier"] = "D"
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="S/A 级证据"):
        validate_executable_plan(plan)

    plan = ready_plan()
    plan["lifecycle"]["transitions"][2], plan["lifecycle"]["transitions"][3] = (
        plan["lifecycle"]["transitions"][3],
        plan["lifecycle"]["transitions"][2],
    )
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="生命周期前缀"):
        validate_executable_plan(plan)


def test_remote_writer_lock_blocks_second_controller(tmp_path):
    class ContendedTransport(FakeTransport):
        def run(self, argv, *, timeout=20, cwd=None, env=None):
            if tuple(argv) == ("mkdir", "/tmp/model-deployment-harness-writer-lock"):
                return CommandResult(tuple(argv), 1, "", "exists")
            return super().run(argv, timeout=timeout, cwd=cwd, env=env)

    with pytest.raises(ExecutionBlocked, match="远端写入锁已被持有"):
        execute_plan(
            ready_plan(),
            ContendedTransport(),
            request=deployment_request(),
            host_profile=observed_host(),
            lock_directory=tmp_path,
            _probe_collector=lambda _: observed_host(),
        )


def test_lifecycle_envelope_requires_typed_hashed_stage_source() -> None:
    plan = ready_plan()
    plan["lifecycle"]["transitions"][0]["artifact"] = {
        "path": "README.md",
        "sha256": file_sha256(ROOT / "README.md"),
        "media_type": "text/markdown",
    }
    plan["review"]["plan_sha256"] = canonical_plan_sha256(plan)
    with pytest.raises(ExecutionBlocked, match="生命周期制品无效"):
        validate_executable_plan(plan)
