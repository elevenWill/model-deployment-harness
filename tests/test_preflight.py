from __future__ import annotations

from scripts.preflight import discovery_gate, host_preflight, requirement_gate


def request():
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
        "model": {"id": "example", "variant": "base"},
        "framework_preference": "sglang",
        "service": {"mode": "container", "bind_host": "127.0.0.1", "port": 30011},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "research",
        "deployment_region": "CN",
    }


def host_profile():
    return {
        "host_id": "knode25",
        "probe": {"status": "COMPLETE"},
        "hardware": {"gpus": [{"index": 0, "uuid": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]},
        "software": {"nvidia": {"cuda_compatibility": "12.4"}},
        "network": {"listening_ports": [22]},
        "runtime": {"gpu_processes": []},
    }


def test_missing_intent_is_needs_user_input_and_never_defaulted():
    incomplete = {"target": {"host": {"address": "10.0.0.25"}}}
    result = requirement_gate(incomplete)
    assert result.status == "NEEDS_USER_INPUT"
    assert "target.gpu_ids" in result.missing_fields
    assert "target.install_root" in result.missing_fields


def test_read_only_discovery_is_not_blocked_by_complete_deployment_intent():
    connection_only = {
        "target": {
            "host": {
                "address": "10.0.0.25",
                "ssh_username": "deploy",
                "ssh_port": 22,
            }
        }
    }
    assert discovery_gate(connection_only).status == "PASS"
    assert requirement_gate(connection_only).status == "NEEDS_USER_INPUT"


def test_ssh_failure_blocks():
    profile = host_profile()
    profile["probe"]["status"] = "FAILED"
    result = host_preflight(
        request(), profile, environment_strategy="container", environment_isolated=True
    )
    assert result.status == "BLOCKED"
    assert "SSH" in result.blockers[0]


def test_gpu_occupation_blocks_without_killing():
    profile = host_profile()
    profile["runtime"]["gpu_processes"] = [
        {
            "gpu_id": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "pid": 99,
            "process_name": "training",
        }
    ]
    result = host_preflight(
        request(), profile, environment_strategy="container", environment_isolated=True
    )
    assert result.status == "BLOCKED"
    assert "未停止任何进程" in " ".join(result.blockers)


def test_cuda_mismatch_blocks_even_when_environment_is_isolated():
    isolated = host_preflight(
        request(),
        host_profile(),
        required_cuda="12.6",
        environment_strategy="container",
        environment_isolated=True,
    )
    assert isolated.status == "BLOCKED"
    assert "无法修复" in isolated.recommendations[0]
    nonisolated = host_preflight(
        request(),
        host_profile(),
        required_cuda="12.6",
        environment_strategy="venv",
        environment_isolated=False,
    )
    assert nonisolated.status == "BLOCKED"
    assert any("CUDA 兼容版本" in blocker for blocker in nonisolated.blockers)


def test_occupied_port_blocks():
    profile = host_profile()
    profile["network"]["listening_ports"].append(30011)
    result = host_preflight(
        request(), profile, environment_strategy="container", environment_isolated=True
    )
    assert result.status == "BLOCKED"
    assert "已在监听" in " ".join(result.blockers)
