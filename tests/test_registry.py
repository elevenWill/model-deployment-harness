from __future__ import annotations

from scripts.registry import attach_observation, deployment_status_view


def _record() -> dict:
    return {
        "schema_version": "1.0",
        "deployment_id": "dep-1",
        "host_id": "host-1",
        "request_ref": "requests/r.json",
        "plan_ref": "deployments/p.json",
        "model": {"id": "example", "variant": "v", "path": "/models/v"},
        "framework": {"name": "example-runtime", "version": "immutable-pin"},
        "target": {
            "gpu_ids": [0],
            "install_root": "/srv/runtime",
            "bind_host": "127.0.0.1",
            "port": 30010,
        },
        "deployment_status": "STARTED",
        "created_at": "2026-08-17T00:00:00Z",
        "updated_at": "2026-08-17T00:00:00Z",
        "known_state": {
            "recorded_at": "2026-08-17T00:00:00Z",
            "expected_service_state": "RUNNING",
            "expected_port": 30010,
        },
    }


def test_known_state_is_not_reported_as_live() -> None:
    view = deployment_status_view(_record())
    assert view["live_check"] is None
    assert "历史" in view["live_summary"]


def test_observed_failure_is_explicit_mismatch() -> None:
    observed = {
        "checked_at": "2026-08-17T01:00:00Z",
        "ssh": "PASS",
        "process": "FAIL",
        "port": "FAIL",
        "api": "NOT_CHECKED",
        "inference": "NOT_CHECKED",
    }
    view = deployment_status_view(attach_observation(_record(), observed))
    assert "已知状态 ≠ 观测状态" in view["live_summary"]
    assert "process" in view["live_summary"]
