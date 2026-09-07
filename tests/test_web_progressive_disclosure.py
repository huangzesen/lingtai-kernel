"""Focused progressive-disclosure guards for the public ``web`` family."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from lingtai.tools.web_search import get_description, get_schema


PACKAGE = Path(__file__).parents[1] / "src" / "lingtai" / "tools" / "web_search"
MANUAL = PACKAGE / "manual" / "SKILL.md"
OPERATION_REFERENCE = PACKAGE / "manual" / "reference" / "operation-contract.md"
SETTINGS = PACKAGE / "settings.py"
WEB_ANATOMY = PACKAGE / "ANATOMY.md"
WEB_CONTRACT = PACKAGE / "CONTRACT.md"
TESTS_ANATOMY = Path(__file__).parent / "ANATOMY.md"
BASE_NONANNOTATION_SHA256 = "04dbfe6d7911e005033e6ba3fc004e99bc8299f45143a9274648eeca888d3c46"


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
    assert "web(action='search', input={'query':'...'})" in description
    assert "web(action='browse', input={...})" in description
    assert "link_ref" in description and "HTTP(S)" in description
    assert "web(action='settings', input={})" in description
    assert "web(action='manual', input={})" in description
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
    # This digest is the canonical serialization of the annotation-stripped
    # schema at immutable migration base 891e7134589652b21e00473b334ac1e439abbf6e.
    # Any structural change must therefore be deliberate and separately reviewed.
    nonannotations = json.dumps(
        _without_annotations(schema),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(nonannotations).hexdigest() == BASE_NONANNOTATION_SHA256


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

    # Keep the moved behavior exact rather than preserving only keywords.
    assert "Supply exactly one public HTTP(S) URL or same-Agent `link_ref`" in manual
    assert "cursor-only input fails `INVALID_TARGET`" in reference
    assert "legacy_fallback_from" in reference
    assert "built-in selector chooses canonical" in reference
    for required_setting_fact in (
        "LINGTAI_WEB_PROVIDER",
        "LINGTAI_WEB_MODEL",
        "rebuilds or relaunches",
        "the next search and a second SHOW",
        "Accepted values are integers `1..100000`",
        "Before lazy service construction",
    ):
        assert required_setting_fact in manual

    operation_path = "src/lingtai/tools/web_search/manual/reference/operation-contract.md"
    test_path = "tests/test_web_progressive_disclosure.py"
    for owner in (WEB_ANATOMY, WEB_CONTRACT):
        owner_text = owner.read_text(encoding="utf-8")
        assert operation_path in owner_text
        assert test_path in owner_text
    assert test_path in TESTS_ANATOMY.read_text(encoding="utf-8")

    citation = re.search(r"manual/SKILL\.md:(\d+)-(\d+)", WEB_ANATOMY.read_text(encoding="utf-8"))
    assert citation is not None
    assert (int(citation.group(1)), int(citation.group(2))) == (1, len(manual.splitlines()))
