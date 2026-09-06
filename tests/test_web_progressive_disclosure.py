"""Focused progressive-disclosure guards for the public ``web`` family."""
from __future__ import annotations

import json
from pathlib import Path

from lingtai.tools.web_search import get_description, get_schema


PACKAGE = Path(__file__).parents[1] / "src" / "lingtai" / "tools" / "web_search"
MANUAL = PACKAGE / "manual" / "SKILL.md"
OPERATION_REFERENCE = PACKAGE / "manual" / "reference" / "operation-contract.md"
SETTINGS = PACKAGE / "settings.py"


def _without_annotations(value):
    """Keep schema structure/constraints while ignoring prose annotations."""
    if isinstance(value, dict):
        return {
            key: _without_annotations(item)
            for key, item in value.items()
            if key not in {"description", "title"}
        }
    if isinstance(value, list):
        return [_without_annotations(item) for item in value]
    return value


def test_web_schema_is_a_first_call_signpost_not_an_operation_manual():
    description = get_description()
    schema = get_schema()
    compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))

    assert len(description) < 400
    assert len(compact) < 3500
    assert "search" in description and "browse" in description
    assert "link_ref" in description and "HTTP(S)" in description
    assert "settings(input={})" in description
    assert "manual(input={})" in description
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["properties"]["action"]["enum"] == [
        "search", "browse", "settings", "manual"
    ]
    browse = next(
        branch for branch in schema["properties"]["input"]["anyOf"]
        if branch["title"] == "browse input"
    )
    assert browse["required"] == ["url", "link_ref", "cursor", "extract", "max_chars"]
    assert browse["properties"]["url"]["type"] == ["string", "null"]
    assert browse["properties"]["max_chars"]["minimum"] == 1
    assert browse["properties"]["max_chars"]["maximum"] == 100000
    # This test intentionally ignores only prose annotations: structural
    # constraints remain the schema's source of truth and are not paraphrased.
    assert _without_annotations(schema)["additionalProperties"] is False


def test_web_manual_routes_depth_and_retains_first_call_safety_contract():
    manual = MANUAL.read_text(encoding="utf-8")
    reference = OPERATION_REFERENCE.read_text(encoding="utf-8")

    assert len(manual) < 12000
    assert "web(action=\"search\"" in manual
    assert "web(action=\"browse\"" in manual
    assert "link_ref" in manual and "cursor" in manual
    assert "public HTTP(S) URL" in manual
    assert "complete document" in manual
    assert "full artifact" in manual
    assert "one named route" in " ".join(manual.split())
    settings_source = SETTINGS.read_text(encoding="utf-8")
    for anchor in (
        "provider", "model", "api-key", "engines", "search-engine",
        "output-max-chars",
    ):
        assert f"#### {anchor}" in manual
        assert f"web-manual#{anchor}" in settings_source
    assert 'f"web-manual#{provider}-api-key"' in settings_source
    for provider in ("openai", "anthropic", "gemini"):
        assert f"#### {provider}-api-key" in manual
    assert "operation-contract.md" in manual
    assert "settings/web.search.json" in reference
    assert "settings/web.json" in reference
    assert "BROWSE_SNAPSHOT_UNAVAILABLE" in reference
    assert "ARTIFACT_WRITE_FAILED" in reference
    assert "SettingsOnlyProviderError" in reference
    assert "RetiredProviderError" in reference
    assert "OAI-SearchBot" in reference
