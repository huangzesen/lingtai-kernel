from __future__ import annotations

import json
from pathlib import Path

from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger


def test_sum_empty_ledger(tmp_path):
    """Sum of a non-existent ledger returns zeros."""
    result = sum_token_ledger(tmp_path / "token_ledger.jsonl")
    assert result == {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_tokens": 0,
        "api_calls": 0,
    }


def test_append_and_sum(tmp_path):
    """Appending entries and summing them returns correct totals."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    append_token_entry(path, input=200, output=100, thinking=30, cached=40)

    result = sum_token_ledger(path)
    assert result == {
        "input_tokens": 300,
        "output_tokens": 150,
        "thinking_tokens": 40,
        "cached_tokens": 60,
        "api_calls": 2,
    }


def test_sum_ignores_corrupt_lines(tmp_path):
    """Corrupt JSONL lines are skipped without error."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    with open(path, "a") as f:
        f.write("not valid json\n")
    append_token_entry(path, input=200, output=100, thinking=30, cached=40)

    result = sum_token_ledger(path)
    assert result == {
        "input_tokens": 300,
        "output_tokens": 150,
        "thinking_tokens": 40,
        "cached_tokens": 60,
        "api_calls": 2,
    }


def test_append_creates_parent_dirs(tmp_path):
    """append_token_entry creates parent directories if missing."""
    path = tmp_path / "logs" / "token_ledger.jsonl"
    append_token_entry(path, input=100, output=50, thinking=10, cached=20)
    assert path.is_file()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["input"] == 100
    assert "ts" in entry


def test_append_entry_has_timestamp(tmp_path):
    """Each entry has a ts field with ISO 8601 UTC timestamp."""
    path = tmp_path / "token_ledger.jsonl"
    append_token_entry(path, input=1, output=2, thinking=3, cached=4)
    entry = json.loads(path.read_text().strip())
    assert "ts" in entry
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
    assert dt.tzinfo is not None
