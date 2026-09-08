"""Shell-language values used by the Bash capability."""
from __future__ import annotations

from dataclasses import dataclass
import enum
import os
import platform
import re
import subprocess
from typing import Any


class ShellKind(enum.Enum):
    """Concrete shell family driving spawn argv and model-facing guidance.

    Values double as the durable ``state_key`` strings produced by each
    dialect, so runtime metadata (async job state, tool description) stays a
    single stable vocabulary shared by the classifier and the dialects.
    """

    POSIX = "posix"
    POWERSHELL = "powershell"
    CMD = "cmd"
    GITBASH = "gitbash"
    WSL = "wsl"

    @classmethod
    def coerce(cls, value: object) -> "ShellKind | None":
        """Accept an enum member or a case-insensitive value string.

        Unknown strings return ``None`` so callers can fall back to the
        platform default instead of failing the whole shell setup.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                return None
        return None

    @classmethod
    def from_state_key(cls, key: object) -> "ShellKind | None":
        """Map a dialect ``state_key()`` value back to its kind."""
        return cls.coerce(key)

    @property
    def display_name(self) -> str:
        """Human-readable shell name for the model-facing description."""
        return _DISPLAY_NAMES[self]

    @property
    def sequencing_guidance(self) -> str:
        """Model-facing sentence teaching chaining/sequencing for this shell."""
        return _SEQUENCING_GUIDANCE[self]


# The single spawn-argument authority.  Every shell family maps to exactly one
# argv template here; dialects build invocations through
# ``make_invocation_for_kind`` so the shape can never drift between the
# model-facing description and ``subprocess``.  POSIX keeps the historical
# ``shell=True`` form (empty template, handled in ``make_invocation_for_kind``)
# so the default platform path is byte-for-byte unchanged.  cmd.exe is the one
# exception: its switches stay in this table, but the actual spawn form is a
# raw pre-joined command-line string (see ``build_cmd_command_line``) because
# cmd cannot parse the MSVC ``list2cmdline`` quoting an argv list would
# produce on Windows.
_SPAWN_ARGV_BY_KIND: dict[ShellKind, tuple[str, ...]] = {
    ShellKind.POSIX: (),
    ShellKind.POWERSHELL: ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command"),
    ShellKind.CMD: ("/d", "/s", "/c"),
    ShellKind.GITBASH: ("-lc",),
    ShellKind.WSL: ("-e", "bash", "-lc"),
}

_DISPLAY_NAMES: dict[ShellKind, str] = {
    ShellKind.POSIX: "Bash (POSIX)",
    ShellKind.POWERSHELL: "PowerShell",
    ShellKind.CMD: "cmd.exe",
    ShellKind.GITBASH: "Git Bash",
    ShellKind.WSL: "WSL bash",
}

_SEQUENCING_GUIDANCE: dict[ShellKind, str] = {
    ShellKind.POSIX: (
        "Chain commands with '&&' (run only on success) or ';' (always run); "
        "'||' runs the next command only on failure."
    ),
    ShellKind.POWERSHELL: (
        "Sequence commands with ';' \u2014 '&&' is not supported by Windows "
        "PowerShell 5.1 and is unsafe to assume; separate pipeline stages with '|'."
    ),
    ShellKind.CMD: (
        "Sequence commands with '&' (always) or '&&' (only on success); "
        "cmd.exe has no ';' statement separator."
    ),
    ShellKind.GITBASH: (
        "Git Bash is Bash: chain with '&&' (run only on success) or ';' (always run)."
    ),
    ShellKind.WSL: (
        "WSL runs Bash: chain with '&&' (run only on success) or ';' (always run)."
    ),
}


# ---------------------------------------------------------------------------
# macOS POSIX spawn policy
# ---------------------------------------------------------------------------
# macOS has been zsh-by-default since Catalina, and apps launched from the
# Finder/Dock inherit launchd's minimal PATH -- without /opt/homebrew/bin
# (Apple Silicon) or /usr/local/bin (Intel) -- so `gh`/`brew` are routinely
# "command not found" inside GUI-launched agents.  On Darwin the POSIX spawn
# form therefore becomes an explicit ``[shell, "-lc", script]`` argv (never
# ``shell=True`` string concatenation) through the user's login shell, which
# restores .zprofile/.zshrc state, and the child env gets the Homebrew bin
# dirs prepended.  Linux keeps the historical ``shell=True`` form unchanged.
_DARWIN_SHELL_PATHS: tuple[str, ...] = ("/bin/zsh", "/bin/bash")
_HOMEBREW_BIN_PATHS: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")

# Credential-shaped env var names stripped from macOS shell children (Claude
# Code ``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`` model): provider API keys, auth/
# OAuth tokens, access keys, and passwords.  General-purpose tokens such as
# ``GITHUB_TOKEN``/``NPM_TOKEN`` are intentionally kept so `gh`/`npm` keep
# working -- only key/secret/password-shaped variables are dropped.
_CREDENTIAL_ENV_RE = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|auth[_-]?token|oauth[_-]?token|"
    r"access[_-]?key|secret[_-]?(?:access[_-]?)?key|secret[_-]?token|"
    r"password)(?:_|$)"
)


def _darwin_default_shell(env: dict[str, str] | None = None) -> str:
    """Resolve the login shell path used for macOS POSIX spawns.

    Order: ``$SHELL`` (when it names a real zsh/bash), then the
    directory-service record for the current user (``dscl . -read
    /Users/<user> UserShell`` -- the authoritative login shell), then
    ``/bin/zsh`` (macOS default since Catalina), then ``/bin/bash``.  This is
    the Codex ``shell_detect`` pattern.  The ``dscl`` probe runs only when
    ``$SHELL`` does not already name a usable zsh/bash (e.g. fish users),
    so the common macOS login shell never pays for a subprocess probe.
    """
    env = os.environ if env is None else env

    def _is_usable(path: str | None) -> bool:
        return bool(path) and os.path.basename(path) in {"zsh", "bash"}

    candidate = env.get("SHELL")
    if _is_usable(candidate):
        return candidate  # type: ignore[return-value]
    user = env.get("USER") or env.get("LOGNAME")
    if user:
        try:
            result = subprocess.run(
                ["dscl", ".", "-read", f"/Users/{user}", "UserShell"],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            line = result.stdout.strip()
            if line:
                shell = line.split()[-1]
                if _is_usable(shell):
                    return shell
    if os.path.exists("/bin/zsh"):
        return "/bin/zsh"
    return "/bin/bash"


_POSIX_SHELL_DISPLAY_BY_BASENAME: dict[str, str] = {"zsh": "Zsh (POSIX)", "bash": "Bash (POSIX)"}


def posix_shell_display_name(env: dict[str, str] | None = None) -> str:
    """Truthful model-facing label for the POSIX ``ShellKind``.

    The static ``Bash (POSIX)`` label is only accurate when the interpreter
    that actually executes the command is bash. On Darwin, execution spawns
    the resolved login shell (:func:`_darwin_default_shell`, zsh by default
    since Catalina), so the description must derive the label from that same
    resolution rather than assert a fixed name that can disagree with reality
    (Lingtai-AI/lingtai#934). Non-Darwin platforms keep the historical static
    label: ``shell=True`` there is unchanged and does not resolve a login
    shell at all.
    """
    if platform.system() != "Darwin":
        return _DISPLAY_NAMES[ShellKind.POSIX]
    basename = os.path.basename(_darwin_default_shell(env))
    return _POSIX_SHELL_DISPLAY_BY_BASENAME.get(basename, _DISPLAY_NAMES[ShellKind.POSIX])


def posix_login_env(
    base_env: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Build the child env for a POSIX login-shell spawn (macOS only).

    GUI-launched macOS apps inherit launchd's minimal PATH; prepend the
    Homebrew bin dirs (``/opt/homebrew/bin`` then ``/usr/local/bin``) when
    they are missing so login shells find ``brew``/``gh``/``python``.  Also
    strips credential-shaped variables (``*_API_KEY``, ``*_TOKEN`` for auth,
    ``*_SECRET*``, ``*_PASSWORD``) so a desktop-launched agent never hands
    provider keys to every command it runs.

    Returns ``None`` on non-Darwin platforms so callers keep the historical
    inherit-the-parent-env behavior unchanged there.
    """
    if platform.system() != "Darwin":
        return None
    env = dict(os.environ if base_env is None else base_env)
    path = env.get("PATH", "")
    parts = path.split(":") if path else []
    missing = [extra for extra in _HOMEBREW_BIN_PATHS if extra not in parts]
    if missing:
        env["PATH"] = ":".join(missing + parts)
    for key in [key for key in env if _CREDENTIAL_ENV_RE.search(key)]:
        env.pop(key)
    return env


def build_cmd_command_line(
    executable: str, switches: tuple[str, ...], script: str,
) -> str:
    """Pre-join the raw command line cmd.exe must receive for *script*.

    cmd.exe is spawned with a raw command-line string, never an argv list:
    on Windows ``subprocess`` joins an argv list with ``list2cmdline``, which
    escapes embedded quotes as MSVC ``\\"`` sequences that cmd.exe does not
    understand (cmd only knows caret ``^`` escaping).  A script as simple as
    ``echo "hello world"`` would otherwise reach cmd corrupted.  The ``/s``
    switch plus a leading space inside the wrapping quotes make cmd's quote
    handling deterministic: under ``/s`` the "exactly two quotes, no specials"
    preserve path never applies, and the wrapper's first/last quotes are the
    only ones the strip rule removes, so the script reaches the shell verbatim
    -- quotes, ``&``/``|`` separators, and all.
    """
    exe_arg = (
        f'"{executable}"'
        if (" " in executable or "\t" in executable)
        else executable
    )
    return f'{exe_arg} {" ".join(switches)} " {script}"'


def make_invocation_for_kind(
    kind: ShellKind, script: str, executable: str | None = None,
) -> "ShellInvocation":
    """Build a spawn form from a ShellKind \u2014 the one spawn-args authority.

    POSIX keeps the historical subprocess ``shell=True`` form so the default
    platform path is byte-for-byte unchanged.  Every other family uses an
    explicit argv template with ``shell=False`` and UTF-8-tolerant text
    decoding, exactly like the PowerShell 7 adapter does today.  cmd.exe is
    the exception: it gets a raw pre-joined command-line string (see
    :func:`build_cmd_command_line`) so embedded quotes survive ``Popen``.
    cmd.exe falls back to ``%COMSPEC%`` (then ``cmd.exe``) when no executable
    is supplied; the other argv families require a discovered executable.
    """
    if kind is ShellKind.POSIX:
        # macOS: spawn the user's login shell with an explicit
        # ``[shell, "-lc", script]`` argv (never ``shell=True`` string
        # concatenation) so .zprofile/.zshrc PATH state is restored for
        # GUI-launched apps, with the Homebrew PATH guarantee attached as the
        # child env.  Linux keeps the historical ``shell=True`` form
        # byte-for-byte so the default platform path is unchanged.
        if platform.system() == "Darwin":
            shell = executable or _darwin_default_shell()
            return ShellInvocation(
                script=script,
                executable=shell,
                argv=("-lc",),
                encoding="utf-8",
                errors="replace",
                env=posix_login_env(),
            )
        return ShellInvocation(script=script)
    if kind is ShellKind.CMD and executable is None:
        executable = os.environ.get("COMSPEC") or "cmd.exe"
    if executable is None:
        raise ValueError(
            f"{kind.value} spawn form requires a discovered executable"
        )
    if kind is ShellKind.CMD:
        return ShellInvocation(
            script=script,
            executable=executable,
            command_line=build_cmd_command_line(
                executable, _SPAWN_ARGV_BY_KIND[ShellKind.CMD], script,
            ),
            encoding="utf-8",
            errors="replace",
        )
    return ShellInvocation(
        script=script,
        executable=executable,
        argv=_SPAWN_ARGV_BY_KIND[kind],
        encoding="utf-8",
        errors="replace",
    )


_POSIX_UNSUPPORTED = "__posix_unsupported__"
_POSIX_RESERVED = {
    "!", "case", "coproc", "do", "done", "elif", "else", "esac", "fi",
    "for", "function", "if", "in", "select", "then", "time", "until",
    "while",
}
_POSIX_CONTROL = {";", "|", "||", "&", "&&", "(", ")", "{", "}", "\n"}
_POSIX_REDIRECTION_RE = re.compile(
    r"^(?:[0-9]{1,3})?(?P<operator>&>>|&>|>>|<<-|<<<|<<|<>|>&|<&|>|<)(?P<target>.*)$"
)
_POSIX_LITERAL_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./:+@=-]+$")


def _posix_redirection(token: str) -> tuple[bool, bool, bool]:
    """Return ``(is_redirection, needs_target, is_heredoc)`` for a token."""
    match = _POSIX_REDIRECTION_RE.fullmatch(token)
    if match is None:
        return False, False, False
    operator = match.group("operator")
    return True, not bool(match.group("target")), operator.startswith("<<")


def _posix_literal_command(token: str) -> bool:
    """Whether *token* is a static executable name, not shell expansion text."""
    return bool(_POSIX_LITERAL_COMMAND_RE.fullmatch(token))


def _posix_command_name(token: str) -> str:
    """Normalize a literal command path to its executable basename for policy."""
    return token.rsplit("/", 1)[-1]


def _balanced_parenthesis(text: str, opening: int) -> int | None:
    """Return the close index for a shell parenthesis, honoring quotes/escapes."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\\\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _peel_posix_expansions(command: str) -> tuple[str, list[str], bool]:
    """Remove executable command substitutions while recursively extracting them."""
    flat: list[str] = []
    nested: list[str] = []
    unsupported = False
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            flat.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            flat.append(char)
            escaped = True
            index += 1
            continue
        if quote == "'":
            flat.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                flat.append(char)
                index += 1
                continue
            # Parameter expansion remains a normal argument; command and
            # arithmetic substitutions are still active inside double quotes.
        elif char in "'\"":
            quote = char
            flat.append(char)
            index += 1
            continue

        if char in "<>" and index + 1 < len(command) and command[index + 1] == "(":
            # Process substitutions execute an opaque command asynchronously;
            # policy cannot prove its command set from this extractor.
            unsupported = True
            flat.extend((char, " "))
            index += 2
            continue
        if (
            char == "$"
            and index + 2 < len(command)
            and command[index + 1:index + 3] == "(("
        ):
            # Arithmetic expansion is evaluated data, not a command body.
            closing = _balanced_parenthesis(command, index + 1)
            if closing is None:
                return "", nested, True
            flat.append(" ")
            index = closing + 1
            continue
        if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            opening = index + 1
            closing = _balanced_parenthesis(command, opening)
            if closing is None:
                return "", nested, True
            inner_commands, inner_unsupported = _extract_posix(command[index + 2:closing])
            nested.extend(inner_commands)
            unsupported = unsupported or inner_unsupported
            flat.append(" ")
            index = closing + 1
            continue
        if char == "`":
            closing = index + 1
            while closing < len(command):
                if command[closing] == "`" and command[closing - 1] != "\\":
                    break
                closing += 1
            if closing >= len(command):
                return "", nested, True
            inner_commands, inner_unsupported = _extract_posix(command[index + 1:closing])
            nested.extend(inner_commands)
            unsupported = unsupported or inner_unsupported
            flat.append(" ")
            index = closing + 1
            continue
        flat.append(char)
        index += 1
    return "".join(flat), nested, unsupported


def _posix_tokens(command: str) -> list[str]:
    """Tokenize enough POSIX shell syntax to identify command positions."""
    tokens: list[str] = []
    word: list[str] = []
    quote: str | None = None
    escaped = False

    def flush() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            # POSIX backslash-newline is a line continuation, not a command
            # separator and not part of the resulting argument.
            if char != "\n":
                word.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            else:
                word.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "#" and not word:
            # A comment begins only at the start of a shell word. Keep the
            # terminating newline so a following command remains visible.
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if char == "\n":
            flush()
            tokens.append("\n")
            index += 1
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        # Braces inside a word are parameter/brace expansion text, not a
        # group delimiter (e.g. ``${HOME}`` and ``{a,b}``). A standalone
        # no-whitespace brace expansion is also one argument; a group opener
        # such as ``{ rm`` remains punctuation.
        if char == "{" and not word:
            closing = command.find("}", index + 1)
            if closing >= index + 1 and not any(c.isspace() for c in command[index + 1:closing]):
                word.append(command[index:closing + 1])
                index = closing + 1
                continue
        if char in "{}" and word:
            word.append(char)
            index += 1
            continue
        # Keep ``>&``/``<&`` and the Bash ``&>`` redirections together rather
        # than treating their ampersand as a command-control operator.
        if char == "&" and word and word[-1] in "<>":
            word.append(char)
            index += 1
            continue
        if char == "&" and not word and index + 1 < len(command) and command[index + 1] == ">":
            word.append("&>")
            index += 2
            continue
        if char in "|&;(){}":
            flush()
            if index + 1 < len(command) and command[index:index + 2] in {"&&", "||", ";;"}:
                tokens.append(command[index:index + 2])
                index += 2
            else:
                tokens.append(char)
                index += 1
            continue
        word.append(char)
        index += 1
    if escaped:
        word.append("\\")
    flush()
    return tokens


def _extract_posix(command: str) -> tuple[list[str], bool]:
    flat, nested, unsupported = _peel_posix_expansions(command)
    tokens = _posix_tokens(flat)
    commands: list[str] = []
    at_command_start = True
    find_command = False
    expect_find_exec_command = False
    find_exec_active = False
    case_pattern = False
    case_active = False
    for_header = False
    redirection_target = False
    saw_redirection = False

    for token in tokens:
        if redirection_target:
            # A control token cannot be a redirection operand. Let it go
            # through the normal control-state transition after recording the
            # malformed redirection; an ordinary next word is just the target.
            if token not in _POSIX_CONTROL and token != ";;":
                redirection_target = False
                continue
            unsupported = True
            redirection_target = False

        if for_header:
            # The loop variable, ``in`` list, separators, and ``do`` are
            # grammar, not executable command positions.
            if token == "do":
                for_header = False
                at_command_start = True
            continue

        if case_pattern:
            # Case alternatives (including ``a|b``) are patterns, never
            # executable names. A closing ``)`` opens the arm command body.
            if token == ")":
                case_pattern = False
                case_active = True
                at_command_start = True
            elif token == "esac":
                case_pattern = False
                case_active = False
                at_command_start = False
            continue

        if token == ";;":
            if case_active:
                case_pattern = True
            at_command_start = True
            find_command = False
            expect_find_exec_command = False
            find_exec_active = False
            continue

        if token in _POSIX_CONTROL:
            # ``find -exec ... \\;`` uses a shell-looking semicolon as the
            # find expression terminator, not as the outer command separator.
            # Keep scanning options belonging to the same find invocation.
            if expect_find_exec_command:
                unsupported = True
                expect_find_exec_command = False
            if token == ";" and find_command and find_exec_active:
                find_exec_active = False
                continue
            at_command_start = True
            find_command = False
            expect_find_exec_command = False
            find_exec_active = False
            continue

        if token == "+" and find_command and find_exec_active:
            if expect_find_exec_command:
                unsupported = True
            find_exec_active = False
            expect_find_exec_command = False
            continue

        if find_command and token in {"-exec", "-execdir"}:
            expect_find_exec_command = True
            find_exec_active = True
            continue

        if expect_find_exec_command:
            # ``{}``, variables, substitutions, and other non-literal words
            # cannot be proven safe as the utility executed by find.
            if not _posix_literal_command(token) or token == "{}":
                unsupported = True
            else:
                commands.append(_posix_command_name(token))
            expect_find_exec_command = False
            continue

        if at_command_start:
            is_redirection, needs_target, is_heredoc = _posix_redirection(token)
            if is_redirection:
                saw_redirection = True
                unsupported = unsupported or is_heredoc
                redirection_target = needs_target
                continue
            if token in _POSIX_RESERVED or re.fullmatch(r"[A-Za-z_]\w*=.*", token):
                if token == "case":
                    case_pattern = True
                    case_active = True
                elif token in {"for", "select"}:
                    for_header = True
                elif token == "esac":
                    case_active = False
                continue
            if not _posix_literal_command(token):
                unsupported = True
                at_command_start = False
                continue
            command_name = _posix_command_name(token)
            commands.append(command_name)
            at_command_start = False
            find_command = command_name == "find"
            expect_find_exec_command = False
            continue

        is_redirection, needs_target, is_heredoc = _posix_redirection(token)
        if is_redirection:
            saw_redirection = True
            unsupported = unsupported or is_heredoc
            redirection_target = needs_target

    if redirection_target or expect_find_exec_command:
        unsupported = True
    # A redirection without an executable is not a command that policy can
    # safely authorize. Comments and grammar-only input, however, remain a
    # harmless empty extraction rather than a synthetic command name.
    if saw_redirection and not commands:
        unsupported = True

    # Preserve ordinary command order; substitutions are appended after the
    # outer command scan while still ensuring every nested command is checked.
    commands.extend(nested)
    return commands, unsupported


def extract_posix_commands(command: str) -> tuple[str, ...]:
    """Extract all provable command names from POSIX compound forms.

    Unknown process substitutions, malformed command substitutions, dynamic
    command-position expansions, and non-empty input with no provable command
    produce a fail-closed sentinel. Parameter/arithmetic expansion and normal
    quoting remain benign arguments.
    """
    commands, unsupported = _extract_posix(command)
    if unsupported:
        commands.append(_POSIX_UNSUPPORTED)
    return tuple(dict.fromkeys(commands))


def _key_subsets(optional: set[str]) -> list[frozenset[str]]:
    """Return every subset of *optional* for ``from_dict`` key-set checks."""
    ordered = sorted(optional)
    subsets: list[frozenset[str]] = []
    for mask in range(1 << len(ordered)):
        subsets.append(
            frozenset(key for i, key in enumerate(ordered) if mask & (1 << i))
        )
    return subsets


@dataclass(frozen=True)
class ShellInvocation:
    """Serializable shell execution form; no cwd, timeout, or result policy.

    ``argv`` and ``command_line`` are mutually exclusive spawn forms:
    ``argv`` is the explicit list form (POSIX/PowerShell/Git Bash/WSL) and
    ``command_line`` is the raw pre-joined string form used only for cmd.exe,
    which cannot parse the backslash-quote escaping ``list2cmdline`` would
    produce from an argv list.
    """

    script: str
    executable: str | None = None
    argv: tuple[str, ...] | None = None
    command_line: str | None = None
    encoding: str | None = None
    errors: str | None = None
    # When set, ``script`` is NOT placed on the child command line; instead the
    # spawner must write ``stdin_script`` to the child's stdin (UTF-8) before
    # waiting.  ``argv`` then carries the complete command line, whose last
    # element is typically an ASCII-only bootstrap that reads stdin.  This is
    # how PowerShell dialects dodge the Windows command-line code page and the
    # 32,768-character process command-line limit.
    stdin_script: str | None = None
    # When set, this exact child env is passed to ``subprocess`` instead of
    # inheriting the parent's.  Used on macOS to prepend the Homebrew bin dirs
    # (GUI-launched PATH guarantee) and strip credential-shaped variables;
    # ``None`` everywhere else keeps the historical inherit-the-parent env.
    env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.script, str) or not self.script.strip():
            raise ValueError("script must be a non-empty string")
        for name in ("executable", "encoding", "errors"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string when present")
        if self.env is not None and (
            not isinstance(self.env, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in self.env.items()
            )
        ):
            raise ValueError("env must be a dict of str to str when present")
        if self.command_line is not None:
            if not isinstance(self.command_line, str) or not self.command_line.strip():
                raise ValueError("command_line must be a non-empty string when present")
            if self.argv is not None:
                raise ValueError("argv and command_line are mutually exclusive spawn forms")
            return
        if self.stdin_script is not None and (
            not isinstance(self.stdin_script, str) or not self.stdin_script.strip()
        ):
            raise ValueError("stdin_script must be a non-empty string when present")
        if self.argv is None:
            return
        if not isinstance(self.argv, (tuple, list)):
            raise ValueError("argv must be a tuple or list of strings")
        if self.executable is None:
            raise ValueError("argv form requires a non-empty executable")
        if not all(isinstance(item, str) for item in self.argv):
            raise ValueError("argv elements must be strings")
        object.__setattr__(self, "argv", tuple(self.argv))

    def to_dict(self) -> dict[str, Any]:
        value = {
            "script": self.script,
            "executable": self.executable,
            "argv": list(self.argv) if self.argv is not None else None,
            "command_line": self.command_line,
            "encoding": self.encoding,
            "errors": self.errors,
        }
        if self.stdin_script is not None:
            value["stdin_script"] = self.stdin_script
        if self.env is not None:
            value["env"] = dict(self.env)
        return value

    @classmethod
    def from_dict(cls, value: object) -> "ShellInvocation | None":
        keys = {"script", "executable", "argv", "encoding", "errors"}
        if not isinstance(value, dict):
            return None
        # ``command_line`` (cmd.exe raw-string form), ``stdin_script``
        # (PowerShell stdin bootstrap) and ``env`` (macOS login-shell child
        # env) are the newer keys; legacy 5-key records without them still
        # load so durable async state written by an older kernel is not
        # rejected.
        optional_keys = {"command_line", "stdin_script", "env"}
        if set(value) not in {
            frozenset(keys | subset) for subset in _key_subsets(optional_keys)
        }:
            return None
        argv = value.get("argv")
        if argv is not None and (
            not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) for item in argv)
        ):
            return None
        command_line = value.get("command_line")
        if command_line is not None and not isinstance(command_line, str):
            return None
        executable = value.get("executable")
        encoding = value.get("encoding")
        errors = value.get("errors")
        stdin_script = value.get("stdin_script")
        env = value.get("env")
        if any(
            item is not None and not isinstance(item, str)
            for item in (executable, encoding, errors, stdin_script)
        ):
            return None
        if env is not None and (
            not isinstance(env, dict)
            or not all(
                isinstance(key, str) and isinstance(val, str)
                for key, val in env.items()
            )
        ):
            return None
        try:
            return cls(
                script=value["script"], executable=executable, argv=argv,
                command_line=command_line, encoding=encoding, errors=errors,
                stdin_script=stdin_script, env=env,
            )
        except (TypeError, ValueError):
            return None

    def process_args(self) -> tuple[object, dict[str, object]]:
        """Return only dialect process arguments; callers add lifecycle policy."""
        if self.command_line is not None:
            args: object = self.command_line
            kwargs: dict[str, object] = {"shell": False}
            if self.executable is not None:
                kwargs["executable"] = self.executable
        elif self.argv is not None:
            args = [self.executable, *self.argv]
            if self.stdin_script is None:
                # Classic argv form: the script is the trailing command argument
                # (for example the payload of ``-Command``).
                args.append(self.script)
            # stdin_script form: ``argv`` already is the complete command line
            # (ending in an ASCII-only bootstrap) and the real script travels
            # through stdin; callers feed ``stdin_script`` before waiting.
            kwargs = {"shell": False}
        elif self.stdin_script is not None:
            # A stdin payload without the argv form has no fixed command line
            # to receive it; fail loudly instead of silently dropping it.
            raise ValueError("stdin_script requires the argv form (non-None argv)")
        else:
            args = self.script
            kwargs = {"shell": True}
            if self.executable is not None:
                kwargs["executable"] = self.executable
        if self.env is not None:
            kwargs["env"] = dict(self.env)
        return args, kwargs


class ShellDialect:
    """Bash-local port for policy extraction and invocation construction."""

    def extract_commands(self, script: str) -> tuple[str, ...]:
        raise NotImplementedError

    def make_invocation(self, script: str) -> ShellInvocation:
        raise NotImplementedError

    def state_key(self) -> str:
        raise NotImplementedError

    def kind(self) -> ShellKind | None:
        """ShellKind for this dialect, or None for unknown/test dialects."""
        return ShellKind.from_state_key(self.state_key())
