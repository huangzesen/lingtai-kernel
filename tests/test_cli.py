import json
import os
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def _posix_scan_pinned():
    """Pin the POSIX ps-scan adapter for guard-policy tests on every platform.

    These tests feed `ps`-shaped rows through a patched
    ``subprocess.check_output``; on Windows the platform selector would return
    the CIM adapter, which speaks JSON, so the ps fixture would silently parse
    to nothing. The Windows scan→guard wiring has its own test in
    ``tests/test_process_scan.py``.
    """
    from lingtai.adapters.posix.process_scan import PosixAgentProcessScanAdapter

    return patch(
        "lingtai.adapters.process_scan.select_agent_process_scan",
        lambda: PosixAgentProcessScanAdapter(),
    )



def _write_init(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a valid init.json to tmp_path and return the path."""
    data = {
        "manifest": {
            "agent_name": "test-agent",
            "language": "en",
            "llm": {
                "provider": "anthropic",
                "model": "test-model",
                "api_key": "test-key",
                "base_url": None,
            },
            "capabilities": {},
            "soul": {"delay": 30},
            "stamina": 60,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 10,
            "admin": {"karma": True},
        },
        "principle": "",
        "covenant": "Be helpful.",
        "pad": "I remember nothing.",
        "lingtai": "",
    }
    if overrides:
        # Deep merge manifest if provided
        for k, v in overrides.items():
            if k == "manifest" and isinstance(v, dict):
                data["manifest"].update(v)
            else:
                data[k] = v
    init_path = tmp_path / "init.json"
    init_path.write_text(json.dumps(data))
    return tmp_path


def test_load_init_reads_file(tmp_path):
    from lingtai.cli import load_init
    _write_init(tmp_path)
    data = load_init(tmp_path)
    assert data["manifest"]["agent_name"] == "test-agent"


def test_load_init_missing_file(tmp_path):
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


def test_load_init_invalid_json(tmp_path):
    (tmp_path / "init.json").write_text("{bad json")
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


def test_load_init_validation_error(tmp_path):
    (tmp_path / "init.json").write_text(json.dumps({"manifest": {}}))
    from lingtai.cli import load_init
    with pytest.raises(SystemExit):
        load_init(tmp_path)


def test_build_agent_fails_when_second_init_read_is_corrupt(tmp_path, capsys):
    from lingtai.cli import build_agent, load_init

    _write_init(tmp_path)
    data = load_init(tmp_path)
    capsys.readouterr()
    (tmp_path / "init.json").write_text("{corrupt json")

    with pytest.raises(SystemExit) as raised:
        build_agent(data, tmp_path)

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert error.startswith("error: ")
    assert json.loads(error.removeprefix("error: "))["read_result"] == "READ_FAILED"


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_constructs_correctly(mock_mail, mock_agent, mock_llm, tmp_path):
    from lingtai.cli import load_init, build_agent
    _write_init(tmp_path)
    data = load_init(tmp_path)

    def assert_construction_sentinel_cleared():
        assert mock_agent.return_value._from_init_boot is False

    mock_agent.return_value._setup_from_init.side_effect = assert_construction_sentinel_cleared
    build_agent(data, tmp_path)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"
    assert llm_kwargs["api_key"] == "test-key"
    assert llm_kwargs["base_url"] is None
    mock_mail.assert_called_once()
    assert mock_mail.call_args.kwargs["working_dir"] == tmp_path
    mock_agent.assert_called_once()
    call_kwargs = mock_agent.call_args
    assert call_kwargs.kwargs["agent_name"] == "test-agent"
    assert call_kwargs.kwargs["admin"] == {"karma": True}
    assert call_kwargs.kwargs["working_dir"] == tmp_path
    # Streaming is System-owned (valid env > valid v2 system.json > fixed false);
    # with neither present the composition root passes the fixed default, never a
    # manifest value.
    assert call_kwargs.kwargs["streaming"] is False
    assert call_kwargs.kwargs["_from_init_boot"] is True
    # covenant, memory, capabilities, addons no longer passed to constructor —
    # they are loaded by _setup_from_init() from init.json
    assert "covenant" not in call_kwargs.kwargs
    assert "pad" not in call_kwargs.kwargs
    assert "capabilities" not in call_kwargs.kwargs
    assert "addons" not in call_kwargs.kwargs
    # _setup_from_init() is called on the constructed agent
    mock_agent.return_value._setup_from_init.assert_called_once()


# --- env file and env var resolution ---


def test_load_env_file(tmp_path):
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text("TEST_CLI_KEY=secret123\nTEST_CLI_OTHER='quoted'\n")

    # Clean up after test
    for k in ("TEST_CLI_KEY", "TEST_CLI_OTHER"):
        os.environ.pop(k, None)

    load_env_file(env_path)
    assert os.environ["TEST_CLI_KEY"] == "secret123"
    assert os.environ["TEST_CLI_OTHER"] == "quoted"

    # Does not overwrite existing by default
    os.environ["TEST_CLI_KEY"] = "original"
    load_env_file(env_path)
    assert os.environ["TEST_CLI_KEY"] == "original"

    # Explicit refresh reloads may choose to overwrite stale process env.
    load_env_file(env_path, overwrite=True)
    assert os.environ["TEST_CLI_KEY"] == "secret123"

    # Clean up
    os.environ.pop("TEST_CLI_KEY", None)
    os.environ.pop("TEST_CLI_OTHER", None)


def test_load_env_file_missing():
    from lingtai.cli import load_env_file
    # Should not raise on missing file
    load_env_file("/nonexistent/.env")


def test_resolve_env_prefers_env_var():
    from lingtai.kernel.config_resolve import resolve_env
    os.environ["TEST_RESOLVE_KEY"] = "from-env"
    try:
        assert resolve_env("raw-value", "TEST_RESOLVE_KEY") == "from-env"
    finally:
        os.environ.pop("TEST_RESOLVE_KEY", None)


def test_resolve_env_falls_back_to_raw():
    from lingtai.kernel.config_resolve import resolve_env
    os.environ.pop("NONEXISTENT_KEY_12345", None)
    assert resolve_env("raw-value", "NONEXISTENT_KEY_12345") == "raw-value"


def test_resolve_env_no_env_name():
    from lingtai.kernel.config_resolve import resolve_env
    assert resolve_env("raw-value", None) == "raw-value"
    assert resolve_env(None, None) is None


def test_load_env_file_export_prefix(tmp_path):
    """`export KEY=val` lines set KEY, never a bogus `export KEY` name."""
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text("export TEST_EXPORT_KEY=bar\n")
    try:
        load_env_file(env_path)
        assert os.environ.get("TEST_EXPORT_KEY") == "bar"
        assert not any(k.startswith("export ") for k in os.environ)
    finally:
        os.environ.pop("TEST_EXPORT_KEY", None)


def test_load_env_file_export_tab_prefix(tmp_path):
    """`export\tKEY=val` (tab separator) also parses."""
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text("export\tTEST_TAB_KEY=baz\n")
    try:
        load_env_file(env_path)
        assert os.environ.get("TEST_TAB_KEY") == "baz"
    finally:
        os.environ.pop("TEST_TAB_KEY", None)


def test_load_env_file_matched_quotes_single_layer(tmp_path):
    """Exactly one layer of symmetric quotes is stripped (dotenv semantics)."""
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text('A="v"\nB=\'v\'\nC=\'"v"\'\n')
    for k in ("A", "B", "C"):
        os.environ.pop(k, None)
    try:
        load_env_file(env_path)
        assert os.environ["A"] == "v"
        assert os.environ["B"] == "v"
        # Inner double quotes survive: one layer only.
        assert os.environ["C"] == '"v"'
    finally:
        for k in ("A", "B", "C"):
            os.environ.pop(k, None)


def test_load_env_file_unmatched_quote_preserved(tmp_path):
    """Values ending/starting in a lone quote round-trip untouched."""
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text('A=abc"\nB="abc\n')
    for k in ("A", "B"):
        os.environ.pop(k, None)
    try:
        load_env_file(env_path)
        assert os.environ["A"] == 'abc"'
        assert os.environ["B"] == '"abc'
    finally:
        for k in ("A", "B"):
            os.environ.pop(k, None)


def test_load_env_file_malformed_key_skipped(tmp_path):
    """A key with an internal space never becomes an env var name."""
    from lingtai.cli import load_env_file
    env_path = tmp_path / ".env"
    env_path.write_text("some words=x\n")
    before = set(os.environ)
    load_env_file(env_path)
    assert set(os.environ) == before


def test_resolve_env_checked_warns_on_miss():
    """A named but unresolvable variable triggers the warn callback."""
    from lingtai.kernel.config_resolve import resolve_env_checked
    os.environ.pop("MISSING_VAR_759", None)
    captured = []
    result = resolve_env_checked(
        None, "MISSING_VAR_759", context="t", warn=captured.append
    )
    assert result is None
    assert captured and "MISSING_VAR_759" in captured[0]


def test_resolve_env_checked_silent_on_hit_and_fallback():
    """No diagnostic when the variable resolves or a fallback value exists."""
    from lingtai.kernel.config_resolve import resolve_env_checked
    os.environ["HIT_VAR_759"] = "yes"
    captured = []
    try:
        assert (
            resolve_env_checked(None, "HIT_VAR_759", context="t", warn=captured.append)
            == "yes"
        )
        assert (
            resolve_env_checked(None, None, context="t", warn=captured.append) is None
        )
        assert (
            resolve_env_checked(
                "fallback", "MISSING_VAR_759", context="t", warn=captured.append
            )
            == "fallback"
        )
    finally:
        os.environ.pop("HIT_VAR_759", None)
    assert captured == []


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_raises_on_unresolvable_api_key_env(
    mock_mail, mock_agent, mock_llm, tmp_path
):
    """A named api_key_env that resolves to nothing fails fast at boot."""
    from lingtai.cli import load_init, build_agent

    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_KEY=value\n")
    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["manifest"]["llm"]["api_key"] = None
    data["manifest"]["llm"]["api_key_env"] = "TEST_LLM_KEY_MISSING"
    data["env_file"] = str(env_file)

    os.environ.pop("TEST_LLM_KEY_MISSING", None)
    try:
        with pytest.raises(ValueError, match="TEST_LLM_KEY_MISSING"):
            build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_LLM_KEY_MISSING", None)


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_resolves_api_key_env(mock_mail, mock_agent, mock_llm, tmp_path):
    """api_key_env resolves from environment, overriding raw api_key."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_LLM_KEY"
    data["manifest"]["llm"]["api_key"] = "fallback-key"

    os.environ["TEST_LLM_KEY"] = "env-key-value"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_LLM_KEY", None)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["api_key"] == "env-key-value"
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_env_file_loaded(mock_mail, mock_agent, mock_llm, tmp_path):
    """env_file is loaded before resolving env vars."""
    from lingtai.cli import load_init, build_agent

    env_path = tmp_path / "secrets.env"
    env_path.write_text("TEST_ENV_FILE_KEY=from-file\n")

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["env_file"] = str(env_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_ENV_FILE_KEY"

    os.environ.pop("TEST_ENV_FILE_KEY", None)
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_ENV_FILE_KEY", None)

    mock_llm.assert_called_once()
    llm_kwargs = mock_llm.call_args.kwargs
    assert llm_kwargs["api_key"] == "from-file"
    assert llm_kwargs["provider"] == "anthropic"
    assert llm_kwargs["model"] == "test-model"


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_env_file_overwrites_on_refresh_marker(mock_mail, mock_agent, mock_llm, tmp_path):
    """Refresh relaunches should let an edited env_file replace stale
    inherited process environment values before resolving api_key_env.
    """
    from lingtai.cli import load_init, build_agent

    env_path = tmp_path / "secrets.env"
    env_path.write_text("TEST_ENV_FILE_REFRESH_KEY=fresh-from-file\n")

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["env_file"] = str(env_path)
    data["manifest"]["llm"]["api_key_env"] = "TEST_ENV_FILE_REFRESH_KEY"

    os.environ["TEST_ENV_FILE_REFRESH_KEY"] = "stale-process-value"
    os.environ["LINGTAI_REFRESH_ENV_OVERWRITE"] = "1"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_ENV_FILE_REFRESH_KEY", None)
        os.environ.pop("LINGTAI_REFRESH_ENV_OVERWRITE", None)

    mock_llm.assert_called_once()
    assert mock_llm.call_args.kwargs["api_key"] == "fresh-from-file"
    assert "LINGTAI_REFRESH_ENV_OVERWRITE" not in os.environ


# --- addons ---


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_passes_addons(mock_mail, mock_agent, mock_llm, tmp_path):
    """Addons from init.json are handled by _setup_from_init, not constructor."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password": "secret",
            "imap_host": "imap.gmail.com",
            "smtp_host": "smtp.gmail.com",
        },
    }

    build_agent(data, tmp_path)

    # Addons no longer passed to constructor — handled by _setup_from_init
    call_kwargs = mock_agent.call_args.kwargs
    assert "addons" not in call_kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_resolves_addon_env(mock_mail, mock_agent, mock_llm, tmp_path):
    """Addon *_env fields are resolved by _setup_from_init via init.json."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password_env": "TEST_IMAP_PASS",
        },
        "telegram": {
            "bot_token_env": "TEST_TG_TOKEN",
        },
    }

    os.environ["TEST_IMAP_PASS"] = "imap-secret"
    os.environ["TEST_TG_TOKEN"] = "tg-secret"
    try:
        build_agent(data, tmp_path)
    finally:
        os.environ.pop("TEST_IMAP_PASS", None)
        os.environ.pop("TEST_TG_TOKEN", None)

    # Addons no longer passed to constructor — handled by _setup_from_init
    assert "addons" not in mock_agent.call_args.kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_no_addons(mock_mail, mock_agent, mock_llm, tmp_path):
    """No addons field — _setup_from_init handles this gracefully."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path)
    data = load_init(tmp_path)
    build_agent(data, tmp_path)

    assert "addons" not in mock_agent.call_args.kwargs
    mock_agent.return_value._setup_from_init.assert_called_once()


def test_log_rebuild_doctor_query_cli(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(json.dumps({"type": "cli_event", "ts": 1}) + "\n", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "log", "rebuild", str(tmp_path)]
        main()
        rebuild_out = json.loads(capsys.readouterr().out)
        assert rebuild_out["status"] == "ok"

        sys.argv = ["lingtai-agent", "log", "doctor", str(tmp_path)]
        main()
        doctor_out = json.loads(capsys.readouterr().out)
        assert doctor_out["event_count"] == 1
        assert doctor_out["chat_entry_count"] == 0

        sys.argv = ["lingtai-agent", "log", "query", str(tmp_path), "SELECT type FROM events"]
        main()
        query_out = json.loads(capsys.readouterr().out)
        assert query_out == [{"type": "cli_event"}]
    finally:
        sys.argv = old_argv


def test_log_query_missing_sqlite_requires_rebuild_cli(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text(json.dumps({"type": "cli_event", "ts": 1}) + "\n", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "log", "query", str(tmp_path), "SELECT type FROM events"]
        try:
            main()
            assert False, "query should exit when sqlite sidecar is missing"
        except SystemExit as exc:
            assert exc.code == 1
        captured = capsys.readouterr()
        assert "rebuild" in captured.err
        assert not (logs / "log.sqlite").exists()
    finally:
        sys.argv = old_argv


def test_maintenance_cleanup_json_cli_reports_candidates(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / ".agent.json").write_text(
        json.dumps({"agent_name": "agent", "admin": {"karma": True}}),
        encoding="utf-8",
    )
    sent = agent / "mailbox" / "sent" / "20260401T010101-abcd"
    sent.mkdir(parents=True)
    (sent / "message.json").write_text("{}", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = [
            "lingtai-agent",
            "maintenance",
            "cleanup",
            str(agent),
            "--older-than-days",
            "30",
            "--json",
        ]
        main()
    finally:
        sys.argv = old_argv

    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "dry_run"
    assert data["classes"]["sent_mail"]["candidates"] == 1
    assert sent.exists()


def test_maintenance_cleanup_human_cli_says_no_files_changed(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / ".agent.json").write_text(
        json.dumps({"agent_name": "agent", "admin": {"karma": True}}),
        encoding="utf-8",
    )

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "maintenance", "cleanup", str(agent)]
        main()
    finally:
        sys.argv = old_argv

    out = capsys.readouterr().out
    assert "Retention cleanup dry-run" in out
    assert "No files were changed" in out


def test_maintenance_cleanup_human_cli_reports_footprints(tmp_path, capsys):
    from lingtai.cli import main
    import sys

    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / ".agent.json").write_text(
        json.dumps({"agent_name": "agent", "admin": {"karma": True}}),
        encoding="utf-8",
    )
    events = agent / "logs" / "events.jsonl"
    events.parent.mkdir()
    events.write_text("{}\n", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = ["lingtai-agent", "maintenance", "cleanup", str(agent)]
        main()
    finally:
        sys.argv = old_argv

    out = capsys.readouterr().out
    assert "footprints: 1" in out
    assert "footprint agent_authoritative_events_log" in out
    assert "risk=authoritative_do_not_delete" in out
    assert "recommendation=Preserve as the authoritative recovery log" in out


def test_maintenance_cleanup_rejects_invalid_days(tmp_path):
    from lingtai.cli import main
    import sys

    old_argv = sys.argv
    try:
        sys.argv = [
            "lingtai-agent",
            "maintenance",
            "cleanup",
            str(tmp_path),
            "--older-than-days",
            "0",
        ]
        with pytest.raises(SystemExit) as exc:
            main()
    finally:
        sys.argv = old_argv

    assert exc.value.code == 2


def test_load_init_reports_legacy_fields_without_mutating_input(tmp_path):
    """CLI boot uses the real reader and leaves compatibility input untouched."""

    from lingtai.cli import load_init

    legacy = "legacy CLI procedures"
    init = {
        "manifest": {
            "agent_name": "cli-agent",
            "llm": {"provider": "p", "model": "m"},
            "capabilities": {},
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "lingtai": "",
        "procedures": legacy,
        "procedures_file": "old/procedures.md",
    }
    (tmp_path / "init.json").write_text(json.dumps(init), encoding="utf-8")

    data = load_init(tmp_path)

    assert data["procedures"] == legacy
    assert data["procedures_file"] == "old/procedures.md"
    on_disk = json.loads((tmp_path / "init.json").read_text(encoding="utf-8"))
    assert on_disk["procedures"] == legacy
    assert on_disk["procedures_file"] == "old/procedures.md"
    assert not (tmp_path / "system" / "migrations").exists()


# ---------------------------------------------------------------------------
# _check_duplicate_process — must match the working-dir argument exactly
# ---------------------------------------------------------------------------


def _ps_line(pid: int, working_dir) -> str:
    """A realistic `ps -eo pid=,command=` line for a `lingtai run <dir>` agent."""
    py = "/usr/local/.../Python"
    return f"{pid} {py} -m lingtai run {working_dir}"


def test_check_duplicate_process_ignores_prefix_sibling(tmp_path):
    """Starting `.../codex` must not be blocked by a running `.../codex_colleague`.

    Regression: `codex` is a prefix of `codex_colleague`, and the old substring
    match treated the colleague's `ps` line as a duplicate codex process.
    """
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    colleague = tmp_path / "codex_colleague"
    codex.mkdir()
    colleague.mkdir()

    ps_out = _ps_line(42779, colleague.resolve()) + "\n"
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=ps_out):
        # Must NOT raise SystemExit — the colleague is a different agent.
        _check_duplicate_process(codex)


def test_check_duplicate_process_detects_exact_match(tmp_path):
    """A genuine same-workdir `lingtai run` process is still flagged."""
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    codex.mkdir()

    ps_out = _ps_line(99999, codex.resolve()) + "\n"
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=ps_out):
        with pytest.raises(SystemExit):
            _check_duplicate_process(codex)


def test_check_duplicate_process_ignores_shell_wrapper(tmp_path):
    """A shell wrapper that merely *evals* the run command is not a duplicate.

    Regression (discussions/covenant-distillation-and-per-agent-profile.md): a
    `zsh -ic '... lingtai run <dir> ...'` wrapper carries the whole command as a
    single quoted argv token, so `run` is never its own token and must not match.
    """
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    codex.mkdir()

    wrapper = f"67192 /bin/zsh -ic 'lingtai run {codex.resolve()} && echo done'"
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=wrapper + "\n"):
        _check_duplicate_process(codex)


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_build_agent_forwards_empty_admin(mock_mail, mock_agent, mock_llm, tmp_path):
    """Empty admin {} from manifest must reach Agent, not be promoted to karma."""
    from lingtai.cli import load_init, build_agent

    _write_init(tmp_path, overrides={"manifest": {"admin": {}}})
    data = load_init(tmp_path)
    build_agent(data, tmp_path)

    call_kwargs = mock_agent.call_args.kwargs
    assert "admin" in call_kwargs, "admin must be forwarded to Agent constructor"
    assert call_kwargs["admin"] == {}


def test_check_duplicate_process_excludes_own_pid(tmp_path):
    """The current process's own `ps` line must never count as a duplicate."""
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    codex.mkdir()

    ps_out = _ps_line(os.getpid(), codex.resolve()) + "\n"
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=ps_out):
        _check_duplicate_process(codex)


def test_check_duplicate_process_excludes_launcher_parent_pid(tmp_path):
    """This process's own launcher parent must never count as a duplicate.

    Regression: a venv launcher stub runs the real interpreter as a child with a
    byte-identical command line, so the stub is our parent AND matches the run
    line. Excluding only `os.getpid()` made the guard report its own launcher.
    """
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    codex.mkdir()

    ps_out = _ps_line(os.getppid(), codex.resolve()) + "\n"
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=ps_out):
        _check_duplicate_process(codex)


def test_check_duplicate_process_still_detects_third_party_beside_own_chain(tmp_path):
    """Excluding self+parent must not blind the guard to a real duplicate."""
    from lingtai.cli import _check_duplicate_process

    codex = tmp_path / "codex"
    codex.mkdir()

    ps_out = (
        _ps_line(os.getpid(), codex.resolve())
        + "\n"
        + _ps_line(os.getppid(), codex.resolve())
        + "\n"
        + _ps_line(99999, codex.resolve())
        + "\n"
    )
    with _posix_scan_pinned(), patch("subprocess.check_output", return_value=ps_out):
        with pytest.raises(SystemExit):
            _check_duplicate_process(codex)


def test_windows_venv_launcher_stub_is_not_a_duplicate(tmp_path, monkeypatch):
    """The real Windows failure: venv stub (our parent) + base interpreter (us).

    Both carry an identical `-m lingtai run <dir>` command line. Observed as
    PID 45676 (stub, ppid 23408) launching PID 36624 (base interpreter), which
    is where the agent code runs -- so the guard saw 45676 and refused to boot.
    """
    import json

    import lingtai.adapters.process_scan as selector
    import lingtai.adapters.windows.process_scan as win_module
    from lingtai.cli import _check_duplicate_process

    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    resolved = working_dir.resolve()
    stub = f'"C:\\venv\\Scripts\\python.exe" -m lingtai run {resolved}'
    base = f'"C:\\Python311\\python.exe" -m lingtai run {resolved}'
    payload = json.dumps(
        [
            {"ProcessId": os.getppid(), "CommandLine": stub},
            {"ProcessId": os.getpid(), "CommandLine": base},
        ]
    )

    monkeypatch.setattr(selector.sys, "platform", "win32")
    monkeypatch.setattr(win_module.shutil, "which", lambda name: "pwsh")
    monkeypatch.setattr(win_module.subprocess, "check_output", lambda *a, **k: payload)
    # Must NOT raise: every match belongs to this process's own launch chain.
    _check_duplicate_process(working_dir)


def test_run_wires_file_logging(monkeypatch, tmp_path):
    """run() must wire setup_logging(log_dir=working_dir/logs) into boot.

    Guards the regression where file logging was dead code: the only sink was
    the stderr console handler, and daemonized agents (stdout=DEVNULL,
    stderr truncated per spawn in logs/spawn.stderr) lost all diagnostics.
    """
    from lingtai import cli
    from lingtai.kernel.logging import get_logger, reset_logging

    reset_logging()

    class _Flag:
        def __init__(self):
            self.was_set = False

        def set(self):
            self.was_set = True

    class _Shutdown:
        def wait(self):
            return None

    class _FakeAgent:
        def __init__(self):
            self._asleep = _Flag()
            self._shutdown = _Shutdown()
            self._state = None
            self._venv_path = None
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self, timeout=10.0):
            self.stopped = True

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "load_init", lambda working_dir: {})
    monkeypatch.setattr(cli, "build_agent", lambda data, working_dir: _FakeAgent())

    import lingtai.venv_resolve as venv_resolve

    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda data: tmp_path / "runtime-venv")

    cli.run(tmp_path)

    log_file = tmp_path / "logs" / "agent.log"
    assert log_file.is_file()

    # The package logger now routes into the file: a warning emitted through
    # any module's get_logger() binding lands in logs/agent.log.
    get_logger().warning("boot wiring marker")
    assert "boot wiring marker" in log_file.read_text(encoding="utf-8")

    reset_logging()


def test_run_marks_derived_avatar_child_as_requiring_authority(monkeypatch, tmp_path):
    """The legacy env marker remains a restrictive redundant boot signal."""
    from lingtai import cli

    class _Flag:
        def set(self):
            pass

    class _Shutdown:
        def wait(self):
            return None

    class _FakeAgent:
        def __init__(self):
            self._asleep = _Flag()
            self._shutdown = _Shutdown()
            self._state = None
            self._venv_path = None

        def start(self):
            pass

        def stop(self, timeout=10.0):
            pass

    captured = {}
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "load_init", lambda working_dir: {})
    def fake_build_agent(data, working_dir, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeAgent()

    monkeypatch.setattr(cli, "build_agent", fake_build_agent)
    import lingtai.venv_resolve as venv_resolve

    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda data: tmp_path / "runtime-venv")
    monkeypatch.setenv("LINGTAI_DERIVED_AVATAR_EXECUTION", "1")

    cli.run(tmp_path)

    assert captured["kwargs"] == {"_requires_derived_launch_admission_port": True}


def test_run_marks_persisted_avatar_child_as_requiring_authority(monkeypatch, tmp_path):
    """A direct later ``lingtai run <child-dir>`` reads durable child state."""
    from lingtai import cli
    from lingtai.tools.avatar._launcher import (
        DERIVED_AVATAR_STATE,
        derived_avatar_state_path,
    )

    class _Flag:
        def set(self):
            pass

    class _Shutdown:
        def wait(self):
            return None

    class _FakeAgent:
        def __init__(self):
            self._asleep = _Flag()
            self._shutdown = _Shutdown()
            self._state = None
            self._venv_path = None

        def start(self):
            pass

        def stop(self, timeout=10.0):
            pass

    captured = {}
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "load_init", lambda working_dir: {})

    def fake_build_agent(data, working_dir, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeAgent()

    monkeypatch.setattr(cli, "build_agent", fake_build_agent)
    import lingtai.venv_resolve as venv_resolve

    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda data: tmp_path / "runtime-venv")
    monkeypatch.delenv("LINGTAI_DERIVED_AVATAR_EXECUTION", raising=False)
    state_path = derived_avatar_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(DERIVED_AVATAR_STATE), encoding="utf-8")

    cli.run(tmp_path)

    assert captured["kwargs"] == {"_requires_derived_launch_admission_port": True}


def test_run_keeps_malformed_persisted_avatar_state_restrictive(monkeypatch, tmp_path):
    """A corrupted derived marker cannot silently restore legacy admission."""
    from lingtai import cli
    from lingtai.tools.avatar._launcher import derived_avatar_state_path

    class _Flag:
        def set(self):
            pass

    class _Shutdown:
        def wait(self):
            return None

    class _FakeAgent:
        def __init__(self):
            self._asleep = _Flag()
            self._shutdown = _Shutdown()
            self._state = None
            self._venv_path = None

        def start(self):
            pass

        def stop(self, timeout=10.0):
            pass

    captured = {}
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "load_init", lambda working_dir: {})

    def fake_build_agent(data, working_dir, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeAgent()

    monkeypatch.setattr(cli, "build_agent", fake_build_agent)
    import lingtai.venv_resolve as venv_resolve

    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda data: tmp_path / "runtime-venv")
    monkeypatch.delenv("LINGTAI_DERIVED_AVATAR_EXECUTION", raising=False)
    state_path = derived_avatar_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not-json", encoding="utf-8")

    cli.run(tmp_path)

    assert captured["kwargs"] == {"_requires_derived_launch_admission_port": True}


def test_derived_avatar_marker_io_error_stays_restrictive(monkeypatch, tmp_path):
    """Only a true missing marker may relax the restart restriction."""
    from lingtai import cli
    from lingtai.tools.avatar._launcher import derived_avatar_state_path

    marker = derived_avatar_state_path(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("present", encoding="utf-8")
    real_lstat = Path.lstat

    def fail_for_marker(path):
        if path == marker:
            raise PermissionError("simulated marker read failure")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_for_marker)
    assert cli._derived_avatar_requires_admission(tmp_path) is True


def test_avatar_child_cli_boot_reaches_structured_missing_authority_denial(
    monkeypatch, tmp_path,
):
    """The real avatar child boot composes required-without-bearer semantics."""
    from lingtai import cli
    from lingtai.agent import Agent
    from lingtai.kernel.provider_admission import (
        DerivedLaunchAdmissionError,
        ProviderAdmissionState,
    )

    _write_init(tmp_path, {"manifest": {"capabilities": {"avatar": {}}}})
    captured = []
    real_build_agent = cli.build_agent

    def capture_build_agent(data, working_dir, **kwargs):
        agent = real_build_agent(data, working_dir, **kwargs)
        captured.append(agent)
        return agent

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "build_agent", capture_build_agent)
    monkeypatch.setattr(Agent, "start", lambda self: self._shutdown.set())
    monkeypatch.setattr(Agent, "stop", lambda self, timeout=10.0: None)
    from lingtai.tools.avatar._launcher import (
        DERIVED_AVATAR_STATE,
        derived_avatar_state_path,
    )

    monkeypatch.delenv("LINGTAI_DERIVED_AVATAR_EXECUTION", raising=False)
    state_path = derived_avatar_state_path(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(DERIVED_AVATAR_STATE), encoding="utf-8")

    cli.run(tmp_path)

    child = captured.pop()
    assert child._requires_derived_launch_admission_port is True
    manager = child.get_capability("avatar")
    with pytest.raises(DerivedLaunchAdmissionError) as raised:
        manager._authorize_derived_launch("nested")
    assert raised.value.decision.state is ProviderAdmissionState.INDETERMINATE
    assert raised.value.decision.reason_code == "required_derived_launch_admission_port_missing"
    records = [
        json.loads(line)
        for line in (tmp_path / "delegates" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records == [
        {
            "ts": records[0]["ts"],
            "event": "avatar_admission_decision",
            "name": "nested",
            "capability": "avatar",
            "state": "indeterminate",
            "reason_code": "required_derived_launch_admission_port_missing",
            "audit_id": None,
        }
    ]


def test_avatar_spawned_directory_stays_restricted_without_launch_env(
    monkeypatch, tmp_path,
):
    """A direct restart of the real spawned child cannot lose its requirement."""
    from lingtai import cli
    from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
    from lingtai.agent import Agent
    from lingtai.kernel.provider_admission import (
        DerivedLaunchAdmissionError,
        DerivedLaunchDecision,
        ProviderAdmissionState,
        RootProviderAdmission,
        bind_provider_admission,
        clear_provider_admission,
    )
    from lingtai.tools.avatar import AvatarManager
    from lingtai.tools.avatar._launcher import AvatarLaunchReceipt, derived_avatar_state_path
    from tests._service_helpers import make_gemini_mock_service

    class _GrantingPort:
        def authorize_derived_launch(self, _parent, _capability):
            return DerivedLaunchDecision(
                ProviderAdmissionState.GRANTED,
                "derived_child_for_restart_test",
                child_endpoint_lease=object(),
            )

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    _write_init(
        parent_dir,
        {"manifest": {"capabilities": {"avatar": {}}}},
    )
    parent = Agent(
        service=make_gemini_mock_service(),
        agent_name="parent",
        working_dir=parent_dir,
        capabilities=["avatar"],
        _turn_origin_policy=RUNTIME_POLICY,
        derived_launch_admission_port=_GrantingPort(),
    )
    parent_avatar = parent.get_capability("avatar")
    parent_avatar._launcher = SimpleNamespace(release=lambda handle: None)
    root = RootProviderAdmission("root-cli-derived-restart", RUNTIME_POLICY.policy_version)
    token = bind_provider_admission(root)
    try:
        with patch.object(
            AvatarManager,
            "_launch",
            return_value=(AvatarLaunchReceipt(12345, object()), tmp_path / "spawn.stderr"),
        ), patch.object(AvatarManager, "_wait_for_boot", return_value=("ok", None)):
            spawned = parent_avatar.handle(
                {"action": "spawn", "input": {"name": "child", "confirm": True}}
            )
    finally:
        clear_provider_admission(token)

    child_dir = parent_dir.parent / "child"
    assert spawned["status"] == "ok"
    assert derived_avatar_state_path(child_dir).is_file()

    captured = []
    real_build_agent = cli.build_agent

    def capture_build_agent(data, working_dir, **kwargs):
        agent = real_build_agent(data, working_dir, **kwargs)
        captured.append(agent)
        return agent

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda working_dir: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda working_dir: None)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda working_dir, agent: None)
    monkeypatch.setattr(cli, "build_agent", capture_build_agent)
    monkeypatch.setattr(Agent, "start", lambda self: self._shutdown.set())
    monkeypatch.setattr(Agent, "stop", lambda self, timeout=10.0: None)
    monkeypatch.delenv("LINGTAI_DERIVED_AVATAR_EXECUTION", raising=False)

    cli.run(child_dir)

    child = captured.pop()
    assert child._requires_derived_launch_admission_port is True
    with pytest.raises(DerivedLaunchAdmissionError) as raised:
        child.get_capability("avatar")._authorize_derived_launch("nested")
    assert raised.value.decision.state is ProviderAdmissionState.INDETERMINATE
    assert raised.value.decision.reason_code == "required_derived_launch_admission_port_missing"


def test_log_doctor_does_not_create_agent_log(monkeypatch, tmp_path):
    """Inspector subcommands must stay write-free w.r.t. agent.log.

    Only run() (the daemon boot path) calls setup_logging(log_dir=...); short-
    lived inspectors like `lingtai-agent log doctor` must not create or touch
    logs/agent.log.
    """
    from lingtai import cli
    from lingtai.kernel.logging import reset_logging

    reset_logging()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    class _Args:
        agent_dir = tmp_path
        log_command = "doctor"

    cli._handle_log_command(_Args())

    assert not (logs_dir / "agent.log").exists()
    reset_logging()
