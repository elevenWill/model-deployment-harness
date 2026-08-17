from __future__ import annotations

from copy import deepcopy

import pytest

import scripts._common as common
from scripts._common import HarnessError, assert_no_secrets, canonical_plan_sha256


def test_plan_hash_ignores_only_hash_slot() -> None:
    plan = {"review": {"status": "APPROVED", "plan_sha256": "a" * 64}, "status": "READY"}
    changed_hash = deepcopy(plan)
    changed_hash["review"]["plan_sha256"] = "b" * 64
    assert canonical_plan_sha256(plan) == canonical_plan_sha256(changed_hash)

    changed_status = deepcopy(plan)
    changed_status["status"] = "EXECUTING"
    assert canonical_plan_sha256(plan) != canonical_plan_sha256(changed_status)


def test_secret_like_fields_cannot_be_persisted() -> None:
    with pytest.raises(HarnessError, match="疑似密钥"):
        assert_no_secrets({"nested": {"password": "do-not-store"}})


def test_secret_environment_values_cannot_be_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "secret-value-123")
    with pytest.raises(HarnessError, match="HF_TOKEN"):
        assert_no_secrets({"command": "download --credential secret-value-123"})


def test_dotenv_only_secret_values_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(common, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("MODEL_TOKEN=dotenv-secret-123\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="MODEL_TOKEN"):
        assert_no_secrets({"output": "dotenv-secret-123"})
