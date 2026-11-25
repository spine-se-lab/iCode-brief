"""Host-neutral interpretation of completed tool observations; never executes tools again."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from compass.evidence.actions import (
    classify_shell,
    contains_test_attempt,
    inspect_action,
    is_test_attempt,
    is_test_command,
)
from compass.evidence.models import EvidenceKind, evidence_excerpt

_MAX_RAW_CAPTURE_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ToolObservation:
    tool_name: str
    tool_kind: str
    arguments: dict = field(default_factory=dict)
    result: str = ""
    failed: bool = False
    exit_code: int | None = None
    working_dir: str = ""
    phase: str = ""
    invocation_id: str = ""
    tool_call_id: str = ""
    action_input: str = ""
    step: int = 0


async def record_observation(tracker, observation: ToolObservation):
    args = observation.arguments
    tool_name, tool_kind = observation.tool_name, observation.tool_kind
    result, exit_code = observation.result, observation.exit_code
    errored = observation.failed or exit_code not in (None, 0)
    target = _target(args)
    command = _command(args) if tool_kind == "shell" else ""
    created_paths = tracker.created_paths()
    modified_paths = tracker.changed_paths()
    test_attempt = bool(command) and is_test_attempt(command)
    if test_attempt and observation.working_dir:
        # A test file written by a program (python -c, a script) never
        # reaches the tracker; git still sees it as untracked or modified.
        modified_paths |= await _git_dirty_paths(observation.working_dir)
    shell_class = (
        classify_shell(command, created_paths=created_paths, modified_paths=modified_paths)
        if tool_kind == "shell"
        else ""
    )
    kind = _classify(tool_kind, shell_class, errored=errored)
    unverified_test_attempt = bool(command and not is_test_command(command) and contains_test_attempt(command))
    if unverified_test_attempt:
        # "ok" still describes the shell tool, not pytest. Do not grant repo-test
        # grounding (or infer pass/fail) from a successful pipeline's last stage.
        shell_class = "other"
        kind = "error" if errored else "test"
    facts = inspect_action(
        tool_name,
        tool_kind,
        args,
        result,
        succeeded=not errored,
        created_paths=created_paths,
        modified_paths=modified_paths,
    )
    summary = _summary(tool_name, kind, target, facts.files, shell_class=shell_class)
    if unverified_test_attempt:
        summary = f"test attempt; test success not established by command status: {target}"
    items = await tracker.record(
        kind=kind,
        outcome="error" if errored else "ok",
        tool_name=tool_name,
        tool_kind=tool_kind,
        action_class=shell_class,
        exit_code=exit_code,
        working_dir=observation.working_dir,
        phase=observation.phase,
        invocation_id=observation.invocation_id,
        tool_call_id=observation.tool_call_id,
        action_input=observation.action_input
        or (command if tool_kind == "shell" else json.dumps(args, ensure_ascii=False, default=str)),
        step=observation.step,
        target=target,
        summary=summary,
        excerpt=evidence_excerpt(result),
        files=facts.files,
        read_range=facts.read_range,
        read_full=facts.read_full,
        opaque_write=facts.opaque_write,
        creation_candidates=facts.creation_candidates,
        deleted_files=facts.deleted_files,
        raw_result=result,
    )
    return items


async def _git_dirty_paths(cwd: str) -> frozenset[str]:
    """Paths git reports as modified or untracked: everything written by any means."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except OSError, TimeoutError:
        return frozenset()
    return frozenset(
        line[3:].split(" -> ")[-1].strip().strip('"')
        for line in out.decode(errors="replace").splitlines()
        if len(line) > 3
    )


def _classify(
    tool_kind: str,
    shell_class: str,
    *,
    errored: bool,
) -> EvidenceKind:
    if errored:
        return "error"
    if shell_class in {"test", "new_test", "self_script"}:
        return "test"
    if tool_kind == "filesystem.write" or shell_class == "change":
        return "change"
    return "observation"


def _target(args: dict[str, object]) -> str:
    for key in ("path", "file_path", "query", "pattern", "glob"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _compact(value)
    return _compact(_command(args))


def _command(args: dict[str, object]) -> str:
    for key in ("cmd", "command", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _summary(
    tool_name: str,
    kind: EvidenceKind,
    target: str,
    files: tuple[str, ...],
    *,
    shell_class: str,
) -> str:
    subject = files[0] if len(files) == 1 else target
    if kind == "observation" and tool_name == "read_file" and subject:
        return f"read {subject}"
    if kind == "test":
        if shell_class == "self_script":
            return f"self-script: {target}" if target else f"self-script: {tool_name}"
        if shell_class == "new_test":
            return f"new-test: {target}" if target else f"new-test: {tool_name}"
        return f"ran tests: {target}" if target else f"{tool_name} ran tests"
    if kind == "change" and subject:
        return f"changed {subject}"
    if kind == "error":
        return f"{tool_name} failed"
    return f"{tool_name} observed"


def _compact(text: str) -> str:
    return " ".join(text[:_MAX_RAW_CAPTURE_CHARS].split())
