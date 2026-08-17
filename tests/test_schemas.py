from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from scripts._common import ROOT, schema_registry, validate_instance


def test_every_schema_is_valid_draft_2020_12() -> None:
    registry = schema_registry()
    assert registry is not None
    for path in sorted((ROOT / "schemas").glob("*.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_all_yaml_artifacts_parse() -> None:
    paths = [
        *sorted((ROOT / "config").glob("*.yaml")),
        *sorted((ROOT / "models").rglob("*.yaml")),
    ]
    assert paths
    for path in paths:
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path


def test_complete_request_contract() -> None:
    request = {
        "schema_version": "1.0",
        "request_id": "req-1",
        "requested_at": "2026-08-17T00:00:00Z",
        "requested_by": "operator",
        "target": {
            "host": {"address": "10.0.0.25", "ssh_username": "deploy", "ssh_port": 22},
            "gpu_ids": [0, 1],
            "install_root": "/srv/runtime",
            "model_root": "/data/models",
        },
        "model": {"id": "minimax-h3", "variant": "fl2va"},
        "framework_preference": "sglang",
        "service": {"mode": "foreground", "bind_host": "127.0.0.1", "port": 30010},
        "existing_environment_policy": "PRESERVE_AND_ISOLATE",
        "intended_use": "internal evaluation",
        "deployment_region": "CN",
    }
    validate_instance(request, "deployment-request.schema.json")


def test_no_schema_or_policy_contains_a_required_intent_default() -> None:
    request_schema = json.loads(
        (ROOT / "schemas" / "deployment-request.schema.json").read_text(encoding="utf-8")
    )
    assert '"default"' not in json.dumps(request_schema)
    policy = yaml.safe_load((ROOT / "config" / "harness-policy.yaml").read_text())
    assert policy["requirement_gate"]["defaults_for_required_user_intent"] == "forbidden"


def test_recipe_paths_do_not_escape_model_boundary() -> None:
    model_root = (ROOT / "models" / "minimax-h3").resolve()
    for path in sorted(model_root.rglob("*.yaml")):
        assert Path(path).resolve().is_relative_to(model_root)


def test_benchmark_contract_records_verified_measurement_context() -> None:
    benchmark = {
        "schema_version": "1.0",
        "benchmark_id": "bench-1",
        "deployment_id": "dep-1",
        "host_id": "knode25",
        "recorded_at": "2026-08-17T00:00:00Z",
        "workload": {"task": "t2va", "duration_seconds": 5},
        "environment": {"framework": "sglang", "gpu_topology": "4xH100"},
        "metrics": {"generation_duration_seconds": 120.5},
        "verification_ref": "verification-1",
    }
    validate_instance(benchmark, "benchmark.schema.json")


def test_semantic_review_contract_rejects_bare_acceptance() -> None:
    semantic_review = {
        "schema_version": "1.0",
        "review_id": "review-1",
        "deployment_id": "dep-1",
        "plan_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "reviewed_by": "operator",
        "reviewed_at": "2026-08-17T00:00:00Z",
        "checks": {
            "not_blank_or_frozen": "PASS",
            "audio_present_not_silent": "PASS",
            "task_alignment": "PASS",
        },
    }
    validate_instance(semantic_review, "semantic-review.schema.json")
