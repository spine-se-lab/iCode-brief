# Copyright (c) 2026 Chrys. All rights reserved.

"""Deterministic action facts used by the evidence Ledger.

The design follows Ledger's ``ActionTracker`` discipline: derive state only
from executed actions and the exact output shown to the worker, fail
conservatively when a write target cannot be proved, and never ask an LLM to
classify or validate evidence.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from compass.evidence.models import EvidenceActionClass

ActionClass = EvidenceActionClass

_FILE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w/])([a-zA-Z0-9_./-]+\."
    r"(?:py|pyx|c|h|cpp|hpp|js|jsx|ts|tsx|md|rst|txt|cfg|toml|yaml|yml|json|sh|ini|csv|html|css|java|go|rs))"
    r"(?![\w/])"
)
_RESULT_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^(?:File:\s+)?([^\s:]+\.(?:py|pyx|c|h|cpp|hpp|js|jsx|ts|tsx|md|rst|txt|cfg|toml|yaml|yml|json|sh|ini|csv|html|css|java|go|rs))(?::\d+|\s|$)"
)
_TRUNCATION_RE: Final[re.Pattern[str]] = re.compile(
    r"\[[^\]\n]*truncated|output (?:of your last command )?(?:was |is )?too long",
    re.IGNORECASE,
)
_CD_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\s*cd\s+\S+\s*&&\s*")
_FILTER_PIPE_RE: Final[re.Pattern[str]] = re.compile(r"\|\s*(?:grep|wc|tail|head|awk|sort|uniq|cut|sed)\b")
_FULL_VIEW_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:cat|nl|less|more)\s+\S+")
_REGION_STAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:sed\s+-n\s+['\"]?\d+(?:,\d+)?p\b|head\s+(?:-n\s*|-)\d+\b)"
)
_FULL_VIEW_STAGE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:cat\s+\S|nl(?:\s+-\S+)*\s+\S)")
_SED_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"\bsed\s+-n\s+['\"]?(\d+),(\d+)p")
_SED_SINGLE_RE: Final[re.Pattern[str]] = re.compile(r"\bsed\s+-n\s+['\"]?(\d+)p")
_HEAD_RE: Final[re.Pattern[str]] = re.compile(r"\bhead\s+(?:-n\s*|-)(\d+)")
_REDIRECT_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9>])(?:1)?>>?\s*(\S+\.\w+)")
_COPY_DEST_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:cp|mv)\s+(?:-\S+\s+)*\S+\s+(\S+\.\w+)")
_TOUCH_RE: Final[re.Pattern[str]] = re.compile(r"\btouch\s+(\S+\.\w+)")
_SED_EDIT_RE: Final[re.Pattern[str]] = re.compile(
    r"\bsed\s+-i\S*\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+(\S+\.\w+)",
    re.IGNORECASE,
)
_GIT_RESTORE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bgit\s+(?:checkout|restore)\b[^\n|;&]*\s(\S+\.\w+)",
    re.IGNORECASE,
)
_DELETE_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|[\s;&|])rm\s+([^|;&>\n]*)")
_SHELL_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON_EXECUTABLE_RE: Final[re.Pattern[str]] = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_ALWAYS_TEST_RUNNERS: Final[frozenset[str]] = frozenset(
    {
        "ansible-test",
        "ctest",
        "jest",
        "nose",
        "nose2",
        "phpunit",
        "py.test",
        "pytest",
        "rspec",
        "runtest.py",
        "runtests.py",
        "tox",
        "vitest",
    }
)
_TEST_SUBCOMMAND_RUNNERS: Final[frozenset[str]] = frozenset({"buck", "cargo", "dotnet", "go", "mix", "swift"})
_TEST_GOAL_RUNNERS: Final[frozenset[str]] = frozenset({"gradle", "gradlew", "make", "mvn", "mvnw"})
_MAKE_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-C", "--directory", "-f", "--file", "-I", "--include-dir", "-o", "--old-file"}
)
_GRADLE_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-c", "--settings-file", "-g", "--gradle-user-home", "-p", "--project-dir"}
)
_MAVEN_VALUE_OPTIONS: Final[frozenset[str]] = frozenset({"-f", "--file", "-s", "--settings", "-t", "--toolchains"})
_BAZEL_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--bazelrc", "--host_jvm_args", "--output_base", "--output_user_root", "--server_javabase"}
)
_PACKAGE_MANAGER_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--cache", "--config", "--cwd", "--dir", "--prefix", "--store-dir"}
)
_UV_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--config-file",
        "--default-index",
        "--directory",
        "--exclude-newer",
        "--extra",
        "--index",
        "--index-strategy",
        "--project",
        "--python",
        "--python-platform",
        "--resolution",
        "--with",
        "--with-editable",
        "--with-requirements",
    }
)
_COMMON_NO_EXECUTION_OPTIONS: Final[frozenset[str]] = frozenset({"-h", "--help", "-V", "--version"})
_PYTEST_NO_EXECUTION_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--co", "--collect-only", "--fixtures", "--fixtures-per-test", "--setup-only", "--setup-plan"}
)
_EXECUTION_INFLUENCING_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "CLASSPATH",
        "COMSPEC",
        "ENV",
        "FPATH",
        "GNUMAKEFLAGS",
        "GOFLAGS",
        "IFS",
        "JDK_JAVA_OPTIONS",
        "JAVA_TOOL_OPTIONS",
        "MAKEFLAGS",
        "MFLAGS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "PATHEXT",
        "PERL5LIB",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
        "RUBYLIB",
        "RUBYOPT",
        "SHELLOPTS",
        "YARN_IGNORE_SCRIPTS",
        "ZDOTDIR",
    }
)
_EXECUTION_INFLUENCING_ENV_PREFIXES: Final[tuple[str, ...]] = ("DYLD_", "LD_", "NPM_CONFIG_")
_TOX_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "-c",
        "--conf",
        "-e",
        "--env",
        "--root",
        "--runner",
        "--workdir",
    }
)


@dataclass(frozen=True, slots=True)
class ActionFacts:
    """Proven state-transition facts for one completed tool call."""

    files: tuple[str, ...] = ()
    read_range: tuple[int, int] | None = None
    read_full: bool = False
    opaque_write: bool = False
    creation_candidates: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()


def phase_name(value: str) -> str:
    """Keep the host-provided provenance label without assigning a workflow."""
    return value.strip()


def inspect_action(
    tool_name: str,
    tool_kind: str,
    arguments: dict[str, object],
    result: str,
    *,
    succeeded: bool,
    created_paths: frozenset[str] = frozenset(),
    modified_paths: frozenset[str] = frozenset(),
) -> ActionFacts:
    """Extract only facts justified by a completed action and visible output."""
    if not succeeded:
        return ActionFacts()

    path = _argument_text(arguments, "path", "file_path")
    if tool_kind == "filesystem.read" and path and tool_name != "view_image":
        line_range = _line_range(arguments.get("line_range"))
        truncated = _TRUNCATION_RE.search(result) is not None
        return ActionFacts(
            files=(normalize_path(path),),
            read_range=line_range if not truncated else None,
            read_full=line_range is None and not truncated,
        )

    if tool_kind == "filesystem.write" and path:
        normalized = normalize_path(path)
        creation_candidates = ()
        if "write" in tool_name.lower():
            creation_candidates = (normalized,)
        return ActionFacts(files=(normalized,), creation_candidates=creation_candidates)

    command = _argument_text(arguments, "cmd", "command", "script")
    if tool_kind != "shell" or not command:
        files = _dedupe([normalize_path(path)] if path else [])
        if tool_kind == "search":
            files = _dedupe([*files, *(normalize_path(item) for item in _RESULT_FILE_RE.findall(result))])
        return ActionFacts(files=files)

    category = classify_shell(command, created_paths=created_paths, modified_paths=modified_paths)
    command_files = extract_files(command)
    if category == "read":
        line_range = extract_read_range(command)
        trustworthy_range = line_range is not None and region_read_is_trustworthy(command)
        full = (
            len(command_files) == 1
            and _FULL_VIEW_RE.search(command) is not None
            and _FILTER_PIPE_RE.search(command) is None
            and _TRUNCATION_RE.search(result) is None
        )
        return ActionFacts(
            files=command_files,
            read_range=line_range if trustworthy_range and _TRUNCATION_RE.search(result) is None else None,
            read_full=full,
        )

    if category == "search":
        result_files = tuple(normalize_path(item) for item in _RESULT_FILE_RE.findall(result))
        return ActionFacts(files=_dedupe([*command_files, *result_files]))

    if category == "change":
        created = _dedupe(
            [
                *(normalize_path(match) for match in _REDIRECT_RE.findall(command)),
                *(normalize_path(match) for match in _COPY_DEST_RE.findall(command)),
                *(normalize_path(match) for match in _TOUCH_RE.findall(command)),
            ]
        )
        deleted = extract_deleted_files(command)
        changed = _dedupe(
            [
                *(normalize_path(match) for match in _SED_EDIT_RE.findall(command)),
                *(normalize_path(match) for match in _GIT_RESTORE_RE.findall(command)),
                *created,
            ]
        )
        return ActionFacts(
            files=_dedupe([*changed, *deleted]),
            opaque_write=not bool(changed or deleted),
            creation_candidates=created,
            deleted_files=deleted,
        )

    # Tests and arbitrary programs can execute repository code and write files
    # without naming every target. Withholding a freshness claim is safer than
    # telling the worker that an old read is still current.
    opaque = category in {"test", "new_test", "self_script", "environment", "reproduce", "other"}
    return ActionFacts(files=command_files, opaque_write=opaque)


def classify_shell(
    command: str,
    *,
    created_paths: frozenset[str] = frozenset(),
    modified_paths: frozenset[str] = frozenset(),
) -> ActionClass:
    """Classify a shell command for Ledger state updates, first match wins.

    ``created_paths`` are files this session created; ``modified_paths`` are
    files it wrote to, created or not. A test run that includes either kind
    of session-authored test source is ``new_test``: a repository test the
    agent edited is no more independent than one it wrote from scratch.
    """
    command = _strip_cd_prefixes(command)
    if re.search(r"\b(?:pip|pip3|conda|mamba|poetry|uv)\s+install|\bapt(?:-get)?\s+install", command):
        return "environment"
    if re.search(
        r"\bsed\s+-i\b|\bgit\s+(?:apply|am)\b|\bpatch\s+-p\d?\b|"
        r"(?<![0-9>])(?:1)?>>?\s*\S+\.\w+|\b(?:cp|mv|touch)\b|"
        r"\bgit\s+(?:checkout|restore)\b[^\n|;&]*\s\S+\.\w+|(?:^|[\s;&|])rm\s+",
        command,
    ):
        return "change"
    test_command = is_test_command(command)
    # Only test-looking edits count: changing production code must not turn
    # the repository tests that cover it into candidate-authored ones.
    session_test_paths = created_paths | frozenset(path for path in modified_paths if _looks_like_test_source(path))
    executed_created_paths = tuple(
        created
        for script_path in _executed_script_paths(command)
        for created in session_test_paths
        if _paths_equivalent(script_path, created)
    )
    named_files = extract_files(command)
    named_created_paths = tuple(
        created
        for named_path in named_files
        for created in session_test_paths
        if _paths_equivalent(named_path, created)
    )
    if test_command and (
        executed_created_paths
        or named_created_paths
        or (not named_files and any(_looks_like_test_source(created) for created in session_test_paths))
    ):
        # A green run that includes candidate-authored tests is useful, but it
        # is not equivalent to a repository-owned oracle that predated the fix.
        return "new_test"
    if any(
        _paths_equivalent(script_path, created)
        for script_path in _executed_script_paths(command)
        for created in created_paths
    ):
        return "self_script"
    if test_command:
        return "test"
    if is_test_attempt(command):
        # A filter after a runner is not a source-code read, and its exit code
        # cannot give the upstream test repository-test grounding.
        return "other"
    if re.search(r"\bpython[\d.]*\s+(?:-c\b|-\s*<<|\S+\.py\b|$)", command):
        return "reproduce"
    if re.search(r"\b(?:cat|head|tail|less|more|nl)\b|\bsed\s+-n\b|\bgit\s+diff\b", command):
        return "read"
    if re.search(r"\b(?:grep|rg|ag)\b|\bfind\b\s+\S+\s+.*-name", command):
        return "search"
    if re.search(r"\b(?:ls|tree|pwd)\b|\bgit\s+(?:status|log|rev-parse|branch|show)\b|^cd\s", command):
        return "safe"
    return "other"


def is_read_only_command(command: str) -> bool:
    """Return whether the deterministic shell classifier considers a command read-only."""
    return classify_shell(command) in {"read", "search", "safe"}


def is_build_command(command: str) -> bool:
    """Recognize direct package builds whose process status is not masked."""
    for stage in _shell_stages(command):
        argv = _unwrap_command_argv(stage)
        if len(argv) < 2:
            continue
        executable = _command_basename(argv[0])
        arguments = argv[1:]
        if _has_option(arguments, _COMMON_NO_EXECUTION_OPTIONS):
            continue
        if executable == "go" and arguments[0] in {"build", "vet"}:
            return True
        if executable == "cargo" and arguments[0] in {"build", "check"}:
            return True
        if executable in {"npm", "pnpm", "yarn"} and arguments[:2] in {("run", "build"), ("run", "typecheck")}:
            return True
    return False


def go_test_covers_file(command: str, *, working_dir: str, path: str) -> bool:
    """Prove only explicit local Go package scope, never inferred dependency coverage.

    Other runners, import-path targets, file-list tests and compound/cd commands
    remain unknown. This is a scope check, not proof of requirement coverage.
    """
    stages = _shell_stages(command)
    if len(stages) != 1 or not working_dir:
        return False
    argv = _unwrap_command_argv(stages[0])
    if len(argv) < 2 or _command_basename(argv[0]) != "go" or argv[1] != "test":
        return False
    arguments = argv[2:]
    if any(arg.endswith(".go") or (arg.startswith("-") and arg not in {"-v", "-race", "-short"}) for arg in arguments):
        return False
    targets = tuple(arg for arg in arguments if arg == "." or arg.startswith("./"))
    if not targets:
        # Default current package is clear only without extra flags/arguments.
        targets = (".",) if not arguments else ()
    for target in targets:
        recursive = target.endswith("/...")
        root = Path(working_dir) / (target[:-4] if recursive else target)
        try:
            relative = Path(path).relative_to(root)
        except ValueError:
            continue
        if ".." not in relative.parts and (recursive or len(relative.parts) == 1 or relative.parts[0] == "testdata"):
            return True
    return False


def is_test_command(command: str) -> bool:
    """Return whether an actual shell stage invokes a recognized test runner.

    Runner names in arguments are deliberately ignored.  This keeps commands
    such as ``echo pytest`` and ``rg 'cargo test'`` from being promoted to test
    evidence while still recognizing option-bearing test-runner invocations.
    Malformed shell input fails closed.
    """
    return _is_test_command(command, depth=0)


def is_test_attempt(command: str) -> bool:
    """Recognize runner stages in pipelines/sequences without claiming they passed."""
    return _is_test_command(command, depth=0, allow_pipes=True)


def _is_test_command(command: str, *, depth: int, allow_pipes: bool = False) -> bool:
    if depth > 4:
        return False
    return any(
        _argv_is_test(stage, depth=depth, allow_pipes=allow_pipes)
        for stage in _shell_stages(command, allow_pipes=allow_pipes)
    )


def _argv_is_test(tokens: tuple[str, ...], *, depth: int, allow_pipes: bool = False) -> bool:
    argv = _unwrap_command_argv(tokens, attempt_only=allow_pipes)
    if not argv:
        return False
    executable = _command_basename(argv[0])
    arguments = argv[1:]

    if executable in {"bash", "sh", "zsh"}:
        script = _shell_command_string(arguments)
        return bool(script) and _is_test_command(script, depth=depth + 1, allow_pipes=allow_pipes)
    if _has_option(arguments, _COMMON_NO_EXECUTION_OPTIONS) or _runner_skips_execution(executable, arguments):
        return False
    if allow_pipes and executable == "timeout" and depth < 4:
        tail = _tail_after_options(arguments, value_options=frozenset({"-k", "--kill-after", "-s", "--signal"}))
        return bool(
            len(tail) > 1
            and re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)[smhd]?", tail[0])
            and _argv_is_test(tail[1:], depth=depth + 1, allow_pipes=True)
        )
    if allow_pipes and executable == "mocha":
        return not _has_option(arguments, frozenset({"--dry-run"}))
    if executable in _ALWAYS_TEST_RUNNERS:
        return True
    if _PYTHON_EXECUTABLE_RE.fullmatch(executable):
        return _python_invokes_test(arguments)
    if executable in {"npm", "pnpm", "yarn"}:
        positionals = _option_positionals(arguments, value_options=_PACKAGE_MANAGER_VALUE_OPTIONS)
        return bool(positionals) and (
            positionals[0] == "test" or (len(positionals) > 1 and positionals[:2] == ("run", "test"))
        )
    if executable in _TEST_SUBCOMMAND_RUNNERS:
        positionals = _option_positionals(arguments)
        return bool(positionals) and positionals[0] == "test"
    if executable == "bazel":
        positionals = _option_positionals(arguments, value_options=_BAZEL_VALUE_OPTIONS)
        return bool(positionals) and positionals[0] == "test"
    if executable in _TEST_GOAL_RUNNERS:
        value_options: frozenset[str] = frozenset()
        joined_value_options: tuple[str, ...] = ()
        if executable == "make":
            value_options = _MAKE_VALUE_OPTIONS
            joined_value_options = ("-C", "-f", "-I", "-o")
        elif executable in {"gradle", "gradlew"}:
            value_options = _GRADLE_VALUE_OPTIONS
            joined_value_options = ("-c", "-g", "-p")
        elif executable in {"mvn", "mvnw"}:
            value_options = _MAVEN_VALUE_OPTIONS
            joined_value_options = ("-f", "-s", "-t")
        goals = _option_positionals(
            arguments,
            value_options=value_options,
            joined_value_options=joined_value_options,
        )
        return any(
            goal == "test" or (executable in {"gradle", "gradlew", "mvn", "mvnw"} and goal.endswith(":test"))
            for goal in goals
        )
    return False


def _shell_stages(command: str, *, allow_pipes: bool = False) -> tuple[tuple[str, ...], ...]:
    """Parse only control flow whose successful status proves every prior stage.

    A simple command and an all-``&&`` chain preserve that implication.  Other
    shell composition can hide a failed test behind a successful final stage,
    so it is rejected rather than partially classified.
    """
    stages: list[tuple[str, ...]] = []
    segments = _status_proving_segments(command, allow_pipes=allow_pipes)
    if not segments:
        return ()
    try:
        for segment in segments:
            lexer = shlex.shlex(segment, posix=True, punctuation_chars="<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = tuple(lexer)
            if not tokens:
                return ()
            stages.append(tokens)
    except ValueError:
        return ()
    return tuple(stages)


def _status_proving_segments(command: str, *, allow_pipes: bool = False) -> tuple[str, ...]:
    segments: list[str] = []
    start = 0
    index = 0
    quote = ""
    redirection_index = -2
    end = len(command)
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = ""
            index += 1
            continue
        if quote == '"':
            if character == "\\":
                index += 2
                continue
            if character == '"':
                quote = ""
                index += 1
                continue
            if character == "`" or (character == "$" and command[index + 1 : index + 2] == "("):
                return ()
            index += 1
            continue
        if character == "\\":
            if index + 1 >= len(command):
                return ()
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "`" or (character == "$" and command[index + 1 : index + 2] == "("):
            return ()
        if character in {"|", ";"} and allow_pipes:
            if command[index + 1 : index + 2] in {"|", "&", ";"}:
                return ()
            segment = command[start:index].strip()
            if not segment:
                return ()
            segments.append(segment)
            start = index + 1
            index += 1
            continue
        if character == "\n" or character in {";", "|", "(", ")"}:
            return ()
        if character == "#" and (index == 0 or command[index - 1].isspace() or command[index - 1] in ";&|()"):
            if "\n" in command[index:]:
                return ()
            end = index
            break
        if character in {">", "<"}:
            redirection_index = index
            index += 1
            continue
        if character != "&":
            index += 1
            continue
        following = command[index + 1 : index + 2]
        if following == "&":
            segment = command[start:index].strip()
            if not segment:
                return ()
            segments.append(segment)
            start = index + 2
            index += 2
            continue
        if redirection_index == index - 1 or following == ">":
            index += 1
            continue
        return ()
    if quote:
        return ()
    tail = command[start:end].strip()
    if not tail:
        return ()
    segments.append(tail)
    return tuple(segments)


def _unwrap_command_argv(tokens: tuple[str, ...], *, attempt_only: bool = False) -> tuple[str, ...]:
    argv = _without_leading_assignments(tokens, attempt_only=attempt_only)
    for _ in range(4):
        if not argv:
            return ()
        executable = _command_basename(argv[0])
        arguments = argv[1:]
        unwrapped: tuple[str, ...] | None = None
        if executable == "env":
            if not attempt_only and _env_changes_execution_context(arguments):
                return ()
            unwrapped = _tail_after_options(
                arguments,
                value_options=frozenset({"-C", "--chdir", "-S", "--split-string", "-u", "--unset"}),
                skip_assignments=True,
                attempt_only=attempt_only,
            )
        elif executable == "command":
            if any(item in {"-p", "-v", "-V"} for item in arguments):
                return argv
            unwrapped = _tail_after_options(arguments)
        elif executable == "uv":
            command_tail = _tail_after_options(arguments, value_options=_UV_VALUE_OPTIONS)
            if command_tail and command_tail[0] == "run":
                unwrapped = _tail_after_options(
                    command_tail[1:],
                    value_options=_UV_VALUE_OPTIONS,
                    skip_assignments=True,
                    attempt_only=attempt_only,
                )
        elif executable in {"pipenv", "poetry"}:
            command_tail = _tail_after_options(
                arguments,
                value_options=frozenset({"-C", "--directory", "--project"}),
            )
            if command_tail and command_tail[0] == "run":
                unwrapped = _without_leading_assignments(command_tail[1:], attempt_only=attempt_only)
        elif executable == "bundle" and arguments[:1] == ("exec",):
            unwrapped = arguments[1:]
        elif executable == "npx":
            unwrapped = _tail_after_options(
                arguments,
                value_options=frozenset({"-c", "--call", "-p", "--package"}),
            )
        if unwrapped is None:
            return argv
        argv = _without_leading_assignments(unwrapped, attempt_only=attempt_only)
    return argv


def _without_leading_assignments(tokens: tuple[str, ...], *, attempt_only: bool = False) -> tuple[str, ...]:
    index = 0
    while index < len(tokens) and _SHELL_ASSIGNMENT_RE.fullmatch(tokens[index]):
        if not attempt_only and _execution_influencing_env_name(tokens[index].split("=", 1)[0]):
            return ()
        index += 1
    return tokens[index:]


def _env_changes_execution_context(arguments: tuple[str, ...]) -> bool:
    """Reject ``env`` forms that can replace the runner or alter its startup."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            for item in arguments[index + 1 :]:
                if not _SHELL_ASSIGNMENT_RE.fullmatch(item):
                    break
                if _execution_influencing_env_name(item.split("=", 1)[0]):
                    return True
            return False
        if argument in {"-i", "--ignore-environment"}:
            return True
        if argument in {"-u", "--unset"}:
            if index + 1 >= len(arguments):
                return True
            if _execution_influencing_env_name(arguments[index + 1]):
                return True
            index += 2
            continue
        if argument.startswith("-u") and not argument.startswith("--") and len(argument) > 2:
            if _execution_influencing_env_name(argument[2:]):
                return True
            index += 1
            continue
        if argument.startswith("--unset="):
            if _execution_influencing_env_name(argument.split("=", 1)[1]):
                return True
            index += 1
            continue
        if _SHELL_ASSIGNMENT_RE.fullmatch(argument):
            if _execution_influencing_env_name(argument.split("=", 1)[0]):
                return True
            index += 1
            continue
        if not argument.startswith("-") or argument == "-":
            return False
        option = argument.split("=", 1)[0]
        index += 1
        if option in {"-C", "--chdir", "-S", "--split-string"} and "=" not in argument:
            index += 1
    return False


def _execution_influencing_env_name(value: str) -> bool:
    normalized = value.strip().upper()
    return normalized in _EXECUTION_INFLUENCING_ENV_NAMES or normalized.startswith(_EXECUTION_INFLUENCING_ENV_PREFIXES)


def _tail_after_options(
    arguments: tuple[str, ...],
    *,
    value_options: frozenset[str] = frozenset(),
    skip_assignments: bool = False,
    attempt_only: bool = False,
) -> tuple[str, ...]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return (
                _without_leading_assignments(arguments[index + 1 :], attempt_only=attempt_only)
                if skip_assignments
                else arguments[index + 1 :]
            )
        if skip_assignments and _SHELL_ASSIGNMENT_RE.fullmatch(argument):
            if not attempt_only and _execution_influencing_env_name(argument.split("=", 1)[0]):
                return ()
            index += 1
            continue
        if not argument.startswith("-") or argument == "-":
            return arguments[index:]
        option = argument.split("=", 1)[0]
        index += 1
        if option in value_options and "=" not in argument and index < len(arguments):
            index += 1
    return ()


def _option_positionals(
    arguments: tuple[str, ...],
    *,
    value_options: frozenset[str] = frozenset(),
    joined_value_options: tuple[str, ...] = (),
) -> tuple[str, ...]:
    result: list[str] = []
    index = 0
    options_ended = False
    while index < len(arguments):
        argument = arguments[index]
        if not options_ended and argument == "--":
            options_ended = True
            index += 1
            continue
        if not options_ended and argument.startswith("-") and argument != "-":
            option = argument.split("=", 1)[0]
            joined_value = any(argument.startswith(prefix) and argument != prefix for prefix in joined_value_options)
            index += 1
            if option in value_options and "=" not in argument and not joined_value and index < len(arguments):
                index += 1
            continue
        result.append(argument)
        index += 1
    return tuple(result)


def _command_basename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _python_invokes_test(arguments: tuple[str, ...]) -> bool:
    try:
        module_index = arguments.index("-m")
    except ValueError:
        module_index = -1
    if module_index >= 0 and module_index + 1 < len(arguments):
        module = arguments[module_index + 1]
        module_arguments = arguments[module_index + 2 :]
        if _runner_skips_execution(module, module_arguments):
            return False
        if module in {"nose", "nose2", "pytest", "tox", "unittest"}:
            return True
        if module == "django":
            return "test" in arguments[module_index + 2 :]
    positionals = _option_positionals(
        arguments,
        value_options=frozenset({"-c", "-m", "-W", "-X"}),
        joined_value_options=("-W", "-X"),
    )
    return bool(positionals) and (
        _command_basename(positionals[0]) in {"runtest.py", "runtests.py"}
        or (_command_basename(positionals[0]) == "manage.py" and "test" in positionals[1:])
    )


def _shell_command_string(arguments: tuple[str, ...]) -> str:
    for index, argument in enumerate(arguments):
        if argument in {"-c", "-lc"} and index + 1 < len(arguments):
            return arguments[index + 1]
    return ""


def _has_option(arguments: tuple[str, ...], options: frozenset[str]) -> bool:
    return any(argument.split("=", 1)[0] in options for argument in arguments)


def _runner_skips_execution(executable: str, arguments: tuple[str, ...]) -> bool:
    if executable in {"pytest", "py.test"}:
        return _has_option(arguments, _PYTEST_NO_EXECUTION_OPTIONS)
    if executable == "cargo":
        harness_arguments = _arguments_after_separator(arguments)
        return _has_option(arguments, frozenset({"--no-run"})) or _has_option(
            harness_arguments,
            frozenset({"--list"}),
        )
    if executable == "go":
        return _has_option(arguments, frozenset({"-list", "--list"}))
    if executable == "dotnet":
        return _has_option(arguments, frozenset({"-t", "--list-tests"}))
    if executable == "make":
        return _has_option(
            arguments,
            frozenset(
                {
                    "-n",
                    "-q",
                    "-t",
                    "--dry-run",
                    "--just-print",
                    "--question",
                    "--recon",
                    "--touch",
                }
            ),
        ) or _has_compact_short_option(arguments, frozenset({"n", "q", "t"}))
    if executable in {"gradle", "gradlew"}:
        return _has_option(arguments, frozenset({"-m", "--dry-run"}))
    if executable in {"mvn", "mvnw"}:
        return any(argument.startswith(("-DskipTests", "-Dmaven.test.skip")) for argument in arguments)
    if executable == "bazel":
        return _has_option(arguments, frozenset({"--nobuild"}))
    if executable == "buck":
        return _has_option(arguments, frozenset({"--dry-run", "--list"}))
    if executable == "ctest":
        return _has_option(arguments, frozenset({"-N", "--print-labels", "--show-only"}))
    if executable == "rspec":
        return _has_option(arguments, frozenset({"--dry-run"}))
    if executable in {"jest", "vitest"}:
        if _has_option(arguments, frozenset({"--clearCache", "--init", "--list", "--listTests", "--showConfig"})):
            return True
        return executable == "vitest" and _first_positional(arguments) == "list"
    if executable == "tox":
        if _has_option(
            arguments,
            frozenset({"-a", "-l", "--list", "--listenvs", "--listenvs-all", "--showconfig"}),
        ):
            return True
        return _first_positional(arguments, value_options=_TOX_VALUE_OPTIONS) in {
            "c",
            "config",
            "d",
            "devenv",
            "exec",
            "l",
            "list",
            "q",
            "quickstart",
        }
    if executable in {"nose", "nose2"}:
        return _has_option(arguments, frozenset({"--collect-only"}))
    if executable == "phpunit":
        return _has_option(
            arguments,
            frozenset(
                {
                    "--dry-run",
                    "--list-groups",
                    "--list-suites",
                    "--list-tests",
                    "--list-tests-xml",
                }
            ),
        )
    if executable == "swift":
        return (
            _has_option(arguments, frozenset({"--list-tests"}))
            or _first_positional(_arguments_after_positional(arguments, "test")) == "list"
        )
    if executable == "ansible-test":
        return _has_option(arguments, frozenset({"--list-targets"}))
    if executable in {"npm", "pnpm", "yarn"}:
        return _has_option(arguments, frozenset({"--dry-run", "--if-present"})) or _has_option(
            _arguments_after_separator(arguments),
            frozenset({"--dry-run", "--list", "--listTests"}),
        )
    return False


def _arguments_after_separator(arguments: tuple[str, ...]) -> tuple[str, ...]:
    try:
        index = arguments.index("--")
    except ValueError:
        return ()
    return arguments[index + 1 :]


def _arguments_after_positional(arguments: tuple[str, ...], value: str) -> tuple[str, ...]:
    try:
        index = arguments.index(value)
    except ValueError:
        return ()
    return arguments[index + 1 :]


def _first_positional(
    arguments: tuple[str, ...],
    *,
    value_options: frozenset[str] = frozenset(),
) -> str:
    positionals = _option_positionals(arguments, value_options=value_options)
    return positionals[0] if positionals else ""


def _has_compact_short_option(arguments: tuple[str, ...], options: frozenset[str]) -> bool:
    return any(
        argument.startswith("-")
        and not argument.startswith("--")
        and argument != "-"
        and bool(set(argument[1:]) & options)
        for argument in arguments
    )


def extract_files(command: str) -> tuple[str, ...]:
    return _dedupe(normalize_path(item) for item in _FILE_PATH_RE.findall(command))


def extract_read_range(command: str) -> tuple[int, int] | None:
    match = _SED_RANGE_RE.search(command)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _SED_SINGLE_RE.search(command)
    if match:
        value = int(match.group(1))
        return value, value
    match = _HEAD_RE.search(command)
    if match:
        return 1, int(match.group(1))
    return None


def region_read_is_trustworthy(command: str) -> bool:
    """Whether an extracted range provably describes unfiltered file lines."""
    stages = command.split("|")
    if len(stages) == 1:
        return _REGION_STAGE_RE.match(stages[0]) is not None
    if len(stages) == 2:
        first, last = stages
        return _FULL_VIEW_STAGE_RE.match(first) is not None and _REGION_STAGE_RE.match(last) is not None
    return False


def extract_deleted_files(command: str) -> tuple[str, ...]:
    deleted: list[str] = []
    for match in _DELETE_RE.finditer(command):
        for token in match.group(1).split():
            if token.startswith("-"):
                continue
            deleted.append(normalize_path(token.rstrip("/")))
    return _dedupe(deleted)


def normalize_path(path: str) -> str:
    clean = path.strip().strip("'\"")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean


def _paths_equivalent(left: str, right: str) -> bool:
    normalized_left, normalized_right = normalize_path(left), normalize_path(right)
    return (
        normalized_left == normalized_right
        or normalized_left.endswith(f"/{normalized_right}")
        or normalized_right.endswith(f"/{normalized_left}")
    )


def _executed_script_paths(command: str, *, _depth: int = 0) -> tuple[str, ...]:
    if _depth > 4:
        return ()
    paths: list[str] = []
    for stage in _shell_stages(command):
        argv = _unwrap_command_argv(stage)
        if not argv:
            continue
        executable = _command_basename(argv[0])
        arguments = argv[1:]
        if executable in {"bash", "sh", "zsh"}:
            nested = _shell_command_string(arguments)
            if nested:
                paths.extend(_executed_script_paths(nested, _depth=_depth + 1))
                continue
            positionals = _option_positionals(arguments)
            if positionals and positionals[0].endswith((".bash", ".py", ".sh")):
                paths.append(positionals[0])
            continue
        if not _PYTHON_EXECUTABLE_RE.fullmatch(executable):
            continue
        try:
            module_index = arguments.index("-m")
        except ValueError:
            module_index = -1
        if module_index >= 0 and module_index + 1 < len(arguments):
            if arguments[module_index + 1] == "pytest":
                paths.extend(extract_files(" ".join(arguments[module_index + 2 :])))
            continue
        positionals = _option_positionals(
            arguments,
            value_options=frozenset({"-c", "-m", "-W", "-X"}),
            joined_value_options=("-W", "-X"),
        )
        if positionals and positionals[0].endswith(".py"):
            paths.append(positionals[0])
    return _dedupe(normalize_path(path) for path in paths)


def _looks_like_test_source(path: str) -> bool:
    candidate = normalize_path(path).lower()
    parts = tuple(part for part in candidate.split("/") if part)
    name = parts[-1] if parts else candidate
    return (
        any(part in {"test", "tests", "spec", "specs"} for part in parts[:-1])
        or name.startswith(("test_", "spec_"))
        or name.endswith(("_test.py", "_test.go", "_test.rs", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


def _strip_cd_prefixes(command: str) -> str:
    stripped = command
    while match := _CD_PREFIX_RE.match(stripped):
        stripped = stripped[match.end() :]
    return stripped


def _line_range(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    start, end = value
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return None
    if not isinstance(end, int) or isinstance(end, bool) or (end != -1 and end < start):
        return None
    return start, end


def _argument_text(arguments: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _dedupe(values) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)


# Compatibility name used by existing host-independent observers.
contains_test_attempt = is_test_attempt
