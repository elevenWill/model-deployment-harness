from __future__ import annotations

import pytest

from scripts._common import HarnessError, validate_instance
from scripts.intake import (
    create_draft,
    evaluate_draft,
    materialize_request,
    merge_draft,
    summarize_draft,
)
from scripts.preflight import discovery_gate, requirement_gate

FIRST_TURN = {
    "target": {"host": {"address": "192.168.121.30", "ssh_port": 22}},
    "model": {"id": "minimax-h3"},
}

SECOND_TURN = {
    "target": {
        "gpu_ids": [1],
        "install_root": "/home/super/wl/db/algo/H3",
        "model_root": "/home/super/wl/db/algo/H3/model",
    },
    "model": {"variant": "both"},
    "service": {"bind_host": "127.0.0.1"},
    "existing_environment_policy": "PRESERVE_AND_ISOLATE",
    "intended_use": "个人研究",
    "deployment_region": "CN",
    "preferences": {
        "download_source": "modelscope",
        "environment_isolation": "isolated_uv",
        "framework_selection": "delegate_to_supported_recipe",
        "port_policy": {
            "strategy": "prefer_default_then_available",
            "preferred_port": 30010,
        },
    },
}


def history_draft() -> dict:
    draft = create_draft(
        "draft-history",
        FIRST_TURN,
        updated_at="2026-08-17T00:00:00Z",
    )
    return merge_draft(
        draft,
        SECOND_TURN,
        updated_at="2026-08-17T00:01:00Z",
    )


def test_partial_draft_is_valid_and_recursively_preserves_previous_turn() -> None:
    draft = history_draft()
    validate_instance(draft, "intake-draft.schema.json")
    assert draft["intent"]["target"]["host"] == {
        "address": "192.168.121.30",
        "ssh_port": 22,
    }
    assert draft["intent"]["target"]["gpu_ids"] == [1]


def test_history_summary_does_not_ask_again_for_confirmed_information() -> None:
    summary = summarize_draft(history_draft())
    assert "你已经提供了服务器地址和 SSH 端口，现在只缺 SSH 登录用户名" in summary
    assert "GPU 编号 1" in summary
    assert "安装目录 /home/super/wl/db/algo/H3" in summary
    assert "模型目录 /home/super/wl/db/algo/H3/model" in summary
    assert "模型的所有变体都需要" in summary
    assert "服务只监听 127.0.0.1" in summary
    assert "部署地区 CN" in summary
    assert "用途 个人研究" in summary
    assert "不需要你重复选择下载源" in summary
    assert "安装方案定稿前还需要确认" not in summary
    assert summary.count("SSH 登录用户名") == 1
    assert "READY" not in summary
    assert "当前草稿不会改动服务器" in summary


def test_after_username_summary_advances_to_discovery_without_future_form() -> None:
    draft = merge_draft(
        history_draft(),
        {"target": {"host": {"ssh_username": "super"}}},
        updated_at="2026-08-17T00:02:00Z",
    )
    summary = summarize_draft(draft)
    assert "现在可以先做只读服务器检查" in summary
    assert "由谁发起" not in summary
    assert "服务如何运行" not in summary
    assert "不需要你现在猜或重复填表" in summary


def test_delegated_framework_and_port_are_resolved_before_final_request() -> None:
    draft = merge_draft(
        history_draft(),
        {
            "requested_by": "operator",
            "target": {"host": {"ssh_username": "super"}},
            "service": {"mode": "foreground"},
        },
        updated_at="2026-08-17T00:02:00Z",
    )
    readiness = evaluate_draft(draft)
    assert readiness.request_status == "PENDING_RESOLUTION"
    assert set(readiness.pending_resolution) == {"framework_preference", "service.port"}

    resolved = merge_draft(
        draft,
        {
            "framework_preference": "vllm-omni",
            "service": {"port": 8091},
        },
        updated_at="2026-08-17T00:03:00Z",
    )
    request = materialize_request(resolved)
    assert request["schema_version"] == "1.0"
    assert request["request_id"] == "draft-history"
    assert request["framework_preference"] == "vllm-omni"
    assert request["service"]["port"] == 8091
    assert evaluate_draft(resolved).request_status == "PASS"


def test_discovery_gate_only_needs_connection_fields() -> None:
    partial = create_draft(
        "discovery",
        {
            "target": {
                "host": {
                    "address": "192.168.121.30",
                    "ssh_username": "super",
                    "ssh_port": 22,
                }
            }
        },
        updated_at="2026-08-17T00:00:00Z",
    )
    result = discovery_gate(partial)
    assert result.status == "PASS"
    assert "known_hosts" in result.recommendations[0]

    readiness = evaluate_draft(partial)
    assert readiness.discovery_status == "PASS"
    assert readiness.request_status == "NEEDS_USER_INPUT"
    assert {
        "requested_by",
        "target.gpu_ids",
        "service.mode",
        "service.port",
    } <= set(readiness.request_missing)
    assert any("许可" in blocker for blocker in readiness.execution_blockers)
    assert any("READY" in blocker for blocker in readiness.execution_blockers)


def test_discovery_gate_rejects_empty_username_and_invalid_port() -> None:
    result = discovery_gate(
        {
            "target": {
                "host": {
                    "address": "192.168.121.30",
                    "ssh_username": "",
                    "ssh_port": 0,
                }
            }
        }
    )
    assert result.status == "NEEDS_USER_INPUT"
    assert result.missing_fields == (
        "target.host.ssh_username",
        "target.host.ssh_port",
    )


def test_full_requirement_gate_is_not_weakened_by_discovery_gate() -> None:
    incomplete = {
        "target": {
            "host": {
                "address": "192.168.121.30",
                "ssh_username": "super",
                "ssh_port": 22,
            }
        }
    }
    assert discovery_gate(incomplete).status == "PASS"
    requirement = requirement_gate(incomplete)
    assert requirement.status == "NEEDS_USER_INPUT"
    assert "requested_by" in requirement.missing_fields
    assert "service.mode" in requirement.missing_fields
    assert "service.port" in requirement.missing_fields


@pytest.mark.parametrize(
    "sensitive_update",
    [
        {"password": "nope"},
        {"target": {"ssh_password": "nope"}},
        {"model_token": "nope"},
        {"preferences": {"access_token": "nope"}},
    ],
)
def test_sensitive_keys_are_rejected(sensitive_update: dict) -> None:
    with pytest.raises(HarnessError, match="密钥字段"):
        create_draft(
            "secret",
            sensitive_update,
            updated_at="2026-08-17T00:00:00Z",
        )


def test_modelscope_is_pending_adaptation_not_user_input() -> None:
    readiness = evaluate_draft(history_draft())
    assert "preferences.download_source" not in readiness.request_missing
    assert any("ModelScope" in blocker for blocker in readiness.execution_blockers)
    assert "下载源" not in [
        # Guard against a future summary implementation accidentally asking again.
        item for item in readiness.request_missing
    ]
