from __future__ import annotations

import yaml

from scripts._common import ROOT, validate_instance


def test_comfyui_source_policy_covers_all_required_families() -> None:
    policy = yaml.safe_load((ROOT / "config/source-policy.yaml").read_text(encoding="utf-8"))

    assert set(policy["source_families"]) >= {
        "comfyui_official",
        "github_issue_pr",
        "pypi_official_package",
        "csdn_technical_validation",
    }
    assert policy["source_families"]["csdn_technical_validation"]["decision_use"] == (
        "reproduction_leads_only"
    )


def test_comfyui_evidence_catalog_has_each_required_source_family() -> None:
    catalog = yaml.safe_load(
        (ROOT / "research/minimax-h3/comfyui-evidence.yaml").read_text(encoding="utf-8")
    )

    assert catalog["schema_version"] == "1.0"
    evidence = catalog["evidence"]
    for item in evidence:
        validate_instance(item, "research-evidence.schema.json")
    assert {item["source"]["source_family"] for item in evidence} >= {
        "comfyui_official",
        "github_issue_pr",
        "pypi_official_package",
        "csdn_technical_validation",
    }
    assert all(
        item["source"]["authority_tier"] in {"C", "D"}
        for item in evidence
        if item["source"]["source_family"] == "csdn_technical_validation"
    )


def test_comfyui_recipe_and_manifest_route_research_to_catalog() -> None:
    manifest = yaml.safe_load(
        (ROOT / "models/minimax-h3/manifest.yaml").read_text(encoding="utf-8")
    )
    recipe = yaml.safe_load(
        (ROOT / "models/minimax-h3/recipes/comfyui.yaml").read_text(encoding="utf-8")
    )

    assert manifest["research"]["comfyui_evidence"] == (
        "../../research/minimax-h3/comfyui-evidence.yaml"
    )
    assert recipe["research"]["evidence_catalog"] == (
        "../../../research/minimax-h3/comfyui-evidence.yaml"
    )
    assert set(recipe["research"]["required_source_families"]) == {
        "comfyui_official",
        "github_issue_pr",
        "pypi_official_package",
        "csdn_technical_validation",
    }
