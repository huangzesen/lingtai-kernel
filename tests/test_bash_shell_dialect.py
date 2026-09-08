"""Contract coverage for the Bash-local shell-language boundary."""
import json
import sys
import time
from types import SimpleNamespace

import pytest

from lingtai.adapters.bash import select_bash_shell_dialect
from lingtai.tools.bash import BashManager, BashPolicy
from lingtai.tools.bash import _BACKGROUND_GUIDANCE, _timeout_error
from lingtai.tools.bash._shell_dialect import ShellDialect, ShellInvocation
from tests._notification_store_helpers import notification_store_for


class MarkerDialect(ShellDialect):
    def extract_commands(self, script):
        return ("blocked",) if script == "marker" else ("echo",)

    def make_invocation(self, script):
        return ShellInvocation(script="printf dialect-marker")

    def state_key(self):
        return "test-marker"


def manager(tmp_path, dialect=None, policy=None):
    return BashManager(
        policy=policy or BashPolicy.yolo(),
        working_dir=str(tmp_path),
        agent=SimpleNamespace(_notification_store=notification_store_for(tmp_path)),
        dialect=dialect,
    )


# PR1 fail-first boundary tests.  These assert the security/correctness
# contract exposed by the manager, not merely tokenizer implementation details.
@pytest.mark.parametrize(
    "command, expected",
    [
        ("echo ok" + "\n" + "rm -f victim", ("echo", "rm")),
        ("echo ok" + chr(92) + "\n" + "rm -f victim", ("echo",)),
        (">out echo ok", ("echo",)),
        ("> out echo ok", ("echo",)),
        ("2>/dev/null echo ok", ("echo",)),
        ("2> /dev/null echo ok", ("echo",)),
        ("2>&1 echo ok", ("echo",)),
        (">out", ("__posix_unsupported__",)),
        ("echo ok >out" + "\n" + "rm -f victim", ("echo", "rm")),
        (r"find . -exec $CMD {} \;", ("find", "__posix_unsupported__")),
        (r"find . -exec {} \;", ("find", "__posix_unsupported__")),
        (r"find . -execdir ${CMD} {} \;", ("find", "__posix_unsupported__")),
        (r"find . -exec echo$CMD {} \;", ("find", "__posix_unsupported__")),
        (r"find . -exec \"$CMD\" {} \;", ("find", "__posix_unsupported__")),
        (r"find . -exec \;", ("find", "__posix_unsupported__")),
        ("for item in one two; do echo ok; done", ("echo",)),
        (
            "case x in a|b) echo ok;; c|d) printf ok;; esac",
            ("echo", "printf"),
        ),
        ("# rm -f victim" + "\n" + "echo ok; # also-not-a-command", ("echo",)),
        ("echo ok; # rm -f victim" + "\n" + "printf ok", ("echo", "printf")),
    ],
)
def test_pr1_posix_extraction_handles_control_grammar_and_dynamic_targets(
    command, expected
):
    assert select_bash_shell_dialect().extract_commands(command) == expected


def test_pr1_manager_policy_enforces_extracted_boundaries(tmp_path):
    dialect = select_bash_shell_dialect()
    allow_echo = manager(tmp_path, dialect=dialect, policy=BashPolicy(allow=["echo"]))
    assert allow_echo.handle({"command": ">out echo ok"})["status"] == "ok"
    assert allow_echo.handle({"command": "echo ok" + "\n" + "rm -f victim"})["status"] == "error"
    assert allow_echo.handle({"command": r"find . -exec $CMD {} \;"})["status"] == "error"
    assert allow_echo.handle({"command": "for item in one; do echo ok; done"})["status"] == "ok"
    assert allow_echo.handle({"command": "case x in a|b) echo ok;; c|d) echo ok;; esac"})["status"] == "ok"
    assert allow_echo.handle({"command": "# rm -f victim" + "\n" + "echo ok"})["status"] == "ok"

    deny_rm = manager(tmp_path, dialect=dialect, policy=BashPolicy(deny=["rm"]))
    assert deny_rm.handle({"command": "echo ok" + "\n" + "printf ok"})["status"] == "ok"
    assert deny_rm.handle({"command": "echo ok" + "\n" + "rm -f victim"})["status"] == "error"
    assert deny_rm.handle({"command": r"find . -exec {} \;"})["status"] == "error"
    assert deny_rm.handle({"command": r"find . -exec /bin/rm {} \;"})["status"] == "error"


@pytest.mark.parametrize(
    "command",
    [
        "rm -f x & echo ok",
        "{ rm -f x; }",
        "(rm -f x)",
        "if true; then rm -f x; fi",
        "while false; do rm -f x; done",
        "for x in one; do rm -f x; done",
        "case x in x) rm -f x;; esac",
        "find . -exec rm -f {} \\;",
        "echo $(printf x; rm -f y)",
    ],
)
def test_posix_compound_extraction_finds_nested_denial(command):
    dialect = select_bash_shell_dialect()
    assert "rm" in dialect.extract_commands(command)


def test_posix_benign_expansions_do_not_trigger_unsupported_marker():
    dialect = select_bash_shell_dialect()
    assert dialect.extract_commands("echo $((1 + 2)) ${HOME} {a,b}") == ("echo",)


def test_posix_unsupported_marker_fails_closed_only_with_policy(tmp_path):
    dialect = select_bash_shell_dialect()
    assert "__posix_unsupported__" in dialect.extract_commands("cat <(echo x)")
    guarded = manager(tmp_path, dialect=dialect, policy=BashPolicy(allow=["cat", "echo"]))
    assert guarded.handle({"command": "cat <(echo x)"})["status"] == "error"
    assert not guarded._policy.is_allowed("cat <(echo x)")
    yolo = manager(tmp_path, dialect=dialect, policy=BashPolicy.yolo())
    assert yolo._validate_command("cat <(echo x)") is None


@pytest.mark.parametrize(
    "command, allowed",
    [
        ("echo 'rm; x' \"echo && rm\"", True),
        (r"echo rm\;x; printf ok", True),
        ("echo $((1 + (2 * 3))) ${HOME} {a,b}", True),
        ("FOO='a b' rm -f x", False),
        ("if true; then rm -f x; fi", False),
        ("while false; do rm -f x; done", False),
        ("for x in one; do rm -f x; done", False),
        ("case x in x) rm -f x;; esac", False),
        (r"find . -exec echo {} \; -exec rm {} +", False),
        ("echo `printf x; rm x`", False),
        ("echo $(printf x; echo $(rm x))", False),
        ("$CMD x", False),
    ],
)
def test_posix_policy_table_keeps_benign_forms_and_denies_compound_forms(
    tmp_path, command, allowed
):
    mgr = manager(tmp_path, dialect=select_bash_shell_dialect(), policy=BashPolicy(deny=["rm"]))
    assert (mgr._validate_command(command) is None) is allowed


def test_posix_quoted_and_escaped_separators_are_arguments_not_commands():
    dialect = select_bash_shell_dialect()
    assert dialect.extract_commands("echo 'rm; x' \"echo && rm\"") == ("echo",)
    assert dialect.extract_commands(r"echo rm\;x; printf ok") == ("echo", "printf")


def test_posix_find_exec_collects_each_literal_target_and_ignores_terminators():
    dialect = select_bash_shell_dialect()
    assert dialect.extract_commands(r"find . -exec echo {} \; -exec rm {} +") == (
        "find", "echo", "rm",
    )


def test_posix_malformed_or_dynamic_command_positions_are_unsupported():
    dialect = select_bash_shell_dialect()
    assert "__posix_unsupported__" in dialect.extract_commands("echo $(printf x")
    assert "__posix_unsupported__" in dialect.extract_commands("$CMD x")


def test_posix_policy_extraction_and_invocation_are_compatible():
    dialect = select_bash_shell_dialect()
    assert dialect.state_key() == "posix"
    assert dialect.extract_commands("FOO=1 echo x | tr x y; $(printf z)") == (
        "echo", "tr", "printf",
    )
    invocation = dialect.make_invocation("echo hello")
    if sys.platform == "darwin":
        # macOS (#1205): spawn through the user's login shell as
        # [shell, "-lc", script] (never shell=True) with UTF-8-tolerant
        # decoding and the login-shell child env; the exact shell path
        # depends on the user's login shell, so only the shape is asserted.
        assert invocation.script == "echo hello"
        assert invocation.executable is not None
        assert invocation.argv == ("-lc",)
        assert invocation.command_line is None
        assert invocation.encoding == "utf-8"
        assert invocation.errors == "replace"
    else:
        # Linux/other POSIX keeps the historical shell=True form: plain record.
        assert invocation.to_dict() == {
            "script": "echo hello", "executable": None, "argv": None,
            "command_line": None, "encoding": None, "errors": None,
        }
    assert ShellInvocation.from_dict(invocation.to_dict()) == invocation


def test_argv_invocation_round_trip_and_process_args_are_unambiguous():
    invocation = ShellInvocation(
        script="Write-Output 'hello'",
        executable="pwsh",
        argv=("-NoProfile", "-Command"),
        encoding="utf-8",
        errors="strict",
    )

    assert ShellInvocation.from_dict(invocation.to_dict()) == invocation
    assert invocation.process_args() == (
        ["pwsh", "-NoProfile", "-Command", "Write-Output 'hello'"],
        {"shell": False},
    )


@pytest.mark.parametrize(
    "field, value",
    [("script", ""), ("script", "   "), ("executable", ""),
     ("encoding", ""), ("errors", "")],
)
def test_invocation_rejects_empty_fields(field, value):
    fields = {"script": "echo hello", "executable": None, "argv": None,
              "encoding": None, "errors": None}
    fields[field] = value
    with pytest.raises(ValueError):
        ShellInvocation(**fields)


def test_argv_requires_executable_and_accepts_list_in_memory():
    with pytest.raises(ValueError):
        ShellInvocation(script="Write-Output hello", argv=("-Command",))
    invocation = ShellInvocation(
        script="Write-Output hello", executable="pwsh", argv=["-Command"]
    )
    assert invocation.argv == ("-Command",)


def test_unknown_or_missing_serialized_keys_are_rejected():
    serialized = ShellInvocation(script="echo hello").to_dict()
    serialized["future"] = True
    assert ShellInvocation.from_dict(serialized) is None
    del serialized["future"]
    del serialized["errors"]
    assert ShellInvocation.from_dict(serialized) is None


@pytest.mark.parametrize(
    "update",
    [{"script": ""}, {"script": "   "}, {"executable": ""},
     {"argv": ["-Command"]}, {"encoding": ""}, {"errors": ""},
     {"argv": ["-Command", 7], "executable": "pwsh"}],
)
def test_invalid_serialized_invocation_returns_none(update):
    serialized = ShellInvocation(script="echo hello").to_dict()
    serialized.update(update)
    assert ShellInvocation.from_dict(serialized) is None


def test_script_process_args_omits_unset_executable():
    assert ShellInvocation(script="echo hello").process_args() == (
        "echo hello", {"shell": True}
    )


def test_selected_dialect_drives_policy_and_sync_execution(tmp_path):
    dialect = MarkerDialect()
    policy = BashPolicy(deny=["blocked"])
    mgr = manager(tmp_path, dialect=dialect, policy=policy)
    assert mgr.handle({"command": "marker"})["status"] == "error"
    result = mgr.handle({"command": "echo"})
    assert result["stdout"] == "dialect-marker"


def test_async_state_persists_dialect_invocation_and_raw_command(tmp_path):
    mgr = manager(tmp_path, dialect=MarkerDialect())
    started = mgr.handle({"command": "echo original", "async": True, "reminder": 30})
    assert started["status"] == "ok"
    state_path = tmp_path / "system" / "jobs" / started["job_id"] / "state.json"
    state = json.loads(state_path.read_text())
    assert state["command"] == "echo original"
    assert state["shell_dialect"] == "test-marker"
    assert ShellInvocation.from_dict(state["invocation"]).script == "printf dialect-marker"
    deadline = time.time() + 3
    while time.time() < deadline:
        if json.loads(state_path.read_text()).get("status") == "completed":
            break
        time.sleep(0.02)
    result = mgr.handle({"action": "poll", "job_id": started["job_id"]})
    assert result["status"] == "done"
    assert result["stdout"] == "dialect-marker"


def test_selector_fails_loudly_on_unsupported_platform(monkeypatch):
    import lingtai.adapters.bash as composition

    monkeypatch.setattr(composition.os, "name", "java")
    with pytest.raises(NotImplementedError, match="unsupported"):
        composition.select_bash_shell_dialect()


# ---------------------------------------------------------------------------
# PR-7: no-output timeout backgrounding guidance (OpenClaw exec-runner.ts
# overall/no-output timeout copy + Hermes foreground hint, adapted to our
# ``async`` parameter name)
# ---------------------------------------------------------------------------


def test_timeout_error_no_output_appends_background_guidance():
    result = _timeout_error("sleep 5", 5.0, no_output=True)
    assert result == {
        "status": "error",
        "message": (
            "Command timed out after 5.0s. Long-running or no-output work should "
            "be launched with async=true (or as a daemon) rather than as a "
            "foreground command; do not rely on shell backgrounding with a "
            "trailing &."
        ),
    }


def test_timeout_error_with_output_also_appends_guidance():
    # Jason 2026-08-10 tool-timeout redesign: the async steering guidance is
    # now appended to *every* timeout result (not only no-output), so the
    # exact historical bare message no longer exists; the prefix is preserved
    # and the guidance is always present.
    assert _timeout_error("echo x", 5.0) == {
        "status": "error",
        "message": (
            "Command timed out after 5.0s. Long-running or no-output work should "
            "be launched with async=true (or as a daemon) rather than as a "
            "foreground command; do not rely on shell backgrounding with a "
            "trailing &."
        ),
    }
    assert _timeout_error("echo x", 5.0, no_output=False) == {
        "status": "error",
        "message": (
            "Command timed out after 5.0s. Long-running or no-output work should "
            "be launched with async=true (or as a daemon) rather than as a "
            "foreground command; do not rely on shell backgrounding with a "
            "trailing &."
        ),
    }


def test_sync_timeout_with_no_output_returns_background_guidance(tmp_path):
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "sleep 5", "timeout": 1.0})
    assert result["status"] == "error"
    assert result["message"].startswith("Command timed out after 1.0s")
    assert _BACKGROUND_GUIDANCE in result["message"]


def test_sync_timeout_with_captured_output_includes_guidance(tmp_path):
    # Jason 2026-08-10 tool-timeout redesign: the async steering guidance is
    # now appended to every timeout result, including one that captured output
    # before the kill.
    mgr = manager(tmp_path)
    command = (
        f"{sys.executable} -u -c \"print('started'); import time; time.sleep(5)\""
    )
    result = mgr.handle({"command": command, "timeout": 1.0})
    assert result["status"] == "error"
    assert result["message"].startswith("Command timed out after 1.0s")
    assert "trailing &" in result["message"]


# ---------------------------------------------------------------------------
# Jason 2026-08-10 tool-timeout redesign: hard ceiling for the sync ``run``
# ``timeout`` parameter (``LINGTAI_TOOL_TIMEOUT_MAX_SECONDS``, default 120).
# ---------------------------------------------------------------------------


def test_resolve_timeout_max_defaults_to_120():
    from lingtai.tools.bash._tool_family import (
        TIMEOUT_MAX_ENV,
        resolve_timeout_max_seconds,
    )

    assert resolve_timeout_max_seconds({}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: ""}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "abc"}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "0"}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "-5"}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "nan"}) == 120.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "inf"}) == 120.0


def test_resolve_timeout_max_reads_env_override():
    from lingtai.tools.bash._tool_family import (
        TIMEOUT_MAX_ENV,
        resolve_timeout_max_seconds,
    )

    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: "300"}) == 300.0
    assert resolve_timeout_max_seconds({TIMEOUT_MAX_ENV: " 45.5 "}) == 45.5


def test_sync_run_refuses_timeout_above_cap(tmp_path, monkeypatch):
    from lingtai.tools.bash._tool_family import TIMEOUT_MAX_ENV

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "120")
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": 300})
    assert result == {
        "status": "error",
        "message": (
            "timeout 300s exceeds the hard ceiling 120s "
            f"({TIMEOUT_MAX_ENV}); for work that may need longer, launch it "
            "with async=true and poll the job instead of raising the sync "
            "timeout."
        ),
    }


def test_sync_run_accepts_timeout_at_cap(tmp_path, monkeypatch):
    from lingtai.tools.bash._tool_family import TIMEOUT_MAX_ENV

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "120")
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": 120})
    assert result["status"] == "ok"
    assert result["exit_code"] == 0


def test_sync_run_cap_follows_env_override(tmp_path, monkeypatch):
    from lingtai.tools.bash._tool_family import TIMEOUT_MAX_ENV

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "30")
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": 31})
    assert result["status"] == "error"
    assert "exceeds the hard ceiling 30s" in result["message"]
    ok = mgr.handle({"command": "echo hi", "timeout": 30})
    assert ok["status"] == "ok"


def test_cap_below_default_is_floored_at_default(tmp_path, monkeypatch):
    # fable r1 BLOCKING-1: a ceiling configured below the 30s default must not
    # refuse every default sync run (the overwhelmingly common case that omits
    # ``timeout``).  The resolver and _handle_run both floor the ceiling at 30.
    from lingtai.tools.bash._tool_family import (
        TIMEOUT_MAX_ENV,
        resolve_timeout_max_seconds,
    )

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "10")
    assert resolve_timeout_max_seconds() == 30.0
    mgr = manager(tmp_path)
    # Omitted timeout → default 30 → accepted because effective cap is 30.
    ok = mgr.handle({"command": "echo hi"})
    assert ok["status"] == "ok"
    # An explicit timeout above the *floored* cap is still refused.
    refused = mgr.handle({"command": "echo hi", "timeout": 31})
    assert refused["status"] == "error"
    assert "exceeds the hard ceiling 30s" in refused["message"]


def test_sync_run_rejects_non_numeric_timeout_without_raising(tmp_path):
    # fable r1 BLOCKING-2: a non-numeric timeout (a common LLM slip) must be a
    # regular error result, never an uncaught TypeError out of handle().
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": "abc"})
    assert result == {"status": "error", "message": "timeout must be a number of seconds"}
    # A numeric string is coerced (so "300" is a number, then refused by the
    # ceiling), never an exception.
    refused = mgr.handle({"command": "echo hi", "timeout": "300"})
    assert refused["status"] == "error"
    assert "exceeds the hard ceiling" in refused["message"]


def test_sync_run_timeout_none_uses_default(tmp_path):
    # ``None`` means *absent* (the family strips null); the default 30 applies
    # and the call runs normally rather than raising.
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": None})
    assert result["status"] == "ok"


def test_sync_run_rejects_negative_timeout(tmp_path):
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "timeout": -5})
    assert result == {
        "status": "error",
        "message": "timeout must be a finite non-negative number of seconds",
    }


def test_async_run_is_exempt_from_timeout_ceiling(tmp_path, monkeypatch):
    # fable r1 NIT-9: async is the escape hatch the refusal message points at;
    # it must stay exempt even with a huge timeout.
    from lingtai.tools.bash._tool_family import TIMEOUT_MAX_ENV

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "120")
    mgr = manager(tmp_path)
    result = mgr.handle({"command": "echo hi", "async": True, "timeout": 9999})
    assert result["status"] == "ok"
    assert "job_id" in result


def test_sync_run_cap_reads_real_process_env(tmp_path, monkeypatch):
    # fable r1 NIT-9: the ``environ is None → os.environ`` branch is exercised
    # through handle() with the real process env, not only injected mappings.
    from lingtai.tools.bash._tool_family import TIMEOUT_MAX_ENV

    monkeypatch.setenv(TIMEOUT_MAX_ENV, "50")
    mgr = manager(tmp_path)
    refused = mgr.handle({"command": "echo hi", "timeout": 51})
    assert refused["status"] == "error"
    assert "exceeds the hard ceiling 50s" in refused["message"]
