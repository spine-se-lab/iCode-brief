# Copyright (c) 2026 Chrys. All rights reserved.

"""Auto-generate the issue-level Goal and inject the current Goal/Claim Brief."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

from compass_brief.model_calls.policy import CallSettings, ModelCallError, ResponseSchemaError, structured_call
from compass_brief.ports import MaintenanceModel
from compass_brief.task_contract.models import (
    MAX_CONCLUSION_TEXT_LENGTH,
    MAX_GOAL_TEXT_LENGTH,
    GoalConclusion,
    GoalState,
)

logger = logging.getLogger(__name__)

# ── Tags ─────────────────────────────────────────────────────────────
# Issue layer (from issue text, permanent)
TAG_OBJECTIVE: Final = "objective"
TAG_SYMPTOM: Final = "symptom"
TAG_REPRODUCTION: Final = "reproduction"
TAG_CRITERIA: Final = "criteria"
TAG_CONSTRAINT: Final = "constraint"
TAG_EXTRACTION: Final = "extraction"
# Analysis conclusions
TAG_ROOT_CAUSE: Final = "root_cause"
TAG_SCOPE: Final = "scope"
# Proposed changes
TAG_FIX: Final = "fix"
# Verification conclusions
TAG_VERIFICATION: Final = "verification"
# Progress markers
TAG_PROGRESS: Final = "progress"

_ISSUE_TAGS: Final = frozenset(
    {TAG_OBJECTIVE, TAG_SYMPTOM, TAG_REPRODUCTION, TAG_CRITERIA, TAG_CONSTRAINT, TAG_EXTRACTION}
)
_ALL_GOAL_TAGS: Final = frozenset(
    {
        TAG_OBJECTIVE,
        TAG_SYMPTOM,
        TAG_REPRODUCTION,
        TAG_CRITERIA,
        TAG_CONSTRAINT,
        TAG_EXTRACTION,
        TAG_ROOT_CAUSE,
        TAG_SCOPE,
        TAG_FIX,
        TAG_VERIFICATION,
    }
)

_TAG_LABELS: Final[dict[str, str]] = {
    TAG_OBJECTIVE: "Objective",
    TAG_SYMPTOM: "Symptom",
    TAG_REPRODUCTION: "Reproduction",
    TAG_CRITERIA: "Requirement",
    TAG_CONSTRAINT: "Constraint",
    TAG_EXTRACTION: "Extraction",
    TAG_ROOT_CAUSE: "Root Cause",
    TAG_SCOPE: "Scope",
    TAG_FIX: "Fix",
    TAG_VERIFICATION: "Verification",
}

_STATUS_MARKS: Final[dict[str, str]] = {
    "confirmed": "+",
    "refuted": "-",
    "pending": "?",
}

# ── Regex patterns ───────────────────────────────────────────────────

_RUNTIME_GOAL_RE = re.compile(
    r"(?:\bCurrently at\b[^.;\n]*[.;]?|\bStep\s+\d+\b[^.;\n]*[.;]?)",
    re.IGNORECASE,
)

_EXPECTED_RE = re.compile(
    r"(?:expected\s*(?:behavior|result|output|outcome)?|should\s+(?:be|return|work|produce))"
    r"\s*[:：\n]\s*(.*?)(?:\n\s*\n|\Z)",  # noqa: RUF001
    re.IGNORECASE | re.DOTALL,
)
_ACTUAL_RE = re.compile(
    r"(?:actual\s*(?:behavior|result|output|outcome)?|(?:crash|error|traceback|exception|raises?|fails?))"
    r"\s*[:：\n]\s*(.*?)(?:\n\s*\n|\Z)",  # noqa: RUF001
    re.IGNORECASE | re.DOTALL,
)
_REPRO_RE = re.compile(
    r"(?:steps?\s*(?:to\s*)?reproduce|how\s*to\s*reproduce|reproduction|reproduce)"
    r"\s*[:：\n]\s*(.*?)(?:\n\s*\n|\Z)",  # noqa: RUF001
    re.IGNORECASE | re.DOTALL,
)

_EXTRACTION_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "title": "IssueRequirementExtraction",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goal": {
            "type": "string",
            "description": "The requested product behavior or outcome, excluding instructions on how to execute the work.",
        },
        "requirements": {
            "type": "array",
            "description": "Behavioral requirements from the issue, including compatible supplied reference clarifications, identified as R1, R2, and so on.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": r"^R\d+$"},
                    "text": {"type": "string"},
                    "reference_derived": {"type": "boolean"},
                },
                "required": ["id", "text", "reference_derived"],
            },
        },
        "constraints": {
            "type": "array",
            "description": "Issue constraints, compatible supplied reference clarifications and observed repository preservation constraints, identified as K1, K2, and so on.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": r"^K\d+$"},
                    "text": {"type": "string"},
                    "reference_derived": {"type": "boolean"},
                },
                "required": ["id", "text", "reference_derived"],
            },
        },
        "signature_changes": {
            "type": "array",
            "description": "Issue-explicit function or method parameter removals.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "symbol": {"type": "string"},
                    "params_named_for_removal": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "unnamed_params_policy": {"type": "string", "enum": ["preserve"]},
                },
                "required": ["symbol", "params_named_for_removal", "unnamed_params_policy"],
            },
        },
    },
    "required": ["goal", "requirements", "constraints", "signature_changes"],
}
_EXTRACTION_SYSTEM_RULES: Final[str] = f"""\
Build the task contract from the issue and any supplied reference and observed repository context.
Preserve every explicit behavioral requirement and constraint from the issue.
- Separate the requested product outcome from instructions to the coding agent, in any language.
  goal describes what is to be built or fixed, never an execution preamble such as how to run commands.
  Instructions about command order, tools or reporting are constraints, not the product goal.
- Mark reference_derived true only for a clause materially clarified by the supplied task_reference_context.
  With no reference context all entries must be false. This records provenance, not evidence or correctness.
- Keep each requirement's complete wording. Do not truncate, summarize away clauses, or merge independent clauses.
- Put externally observable behavior and capabilities only in requirements, with sequential R* schema IDs.
- Execution instructions may precede or follow the issue. Keep its behavioral requirements even when the user
  adds restrictions on tools, network, reference solutions, or workflow. These restrictions are separate K* entries.
- Mentioning an existing function or file does NOT turn its required behavior into a constraint:
  "parser should return milliseconds" and "invalid input should raise ValueError" are R* requirements.
- A behavior is still a requirement when it uses "must", names a function, or appears beside execution constraints.
  Split behavior from implementation/preservation clauses when both occur. Do not put all behavior into constraints.
- Put limitations, prohibitions, invariants, compatibility/preservation clauses, and named implementation deliverables
  only in constraints, with sequential K* schema IDs. Never put a constraint in requirements.
- A named file, function, method, type, field, config key, fixture, signature, or other deliverable that the issue asks
  the implementation to create, expose, or preserve MUST be returned as a complete constraint.
  This covers its identity, shape and preservation, not every behavior mentioning its name:
  "parse(data) returns [] for empty input" is a requirement; "preserve parse(data)'s signature" is a constraint.
  Preserve every exact repository-relative path, symbol spelling, signature, visibility, and underlying type stated by
  the issue in that constraint's text. A task that only asks for named deliverables must still return constraints.
- Treat explicit negative and preservation wording such as "must not", "do not", "no new", "preserve", "remain", and
  "keep existing" as constraints. In particular, a field such as `New interfaces introduced:` whose value says
  `No new interfaces are introduced` MUST produce a K* constraint preserving that complete statement.
- When the issue explicitly says that named parameters of an existing function or method must be removed or made
  internal, add one signature_changes entry. Put ONLY parameter names stated by the issue in
  params_named_for_removal. Set unnamed_params_policy to preserve: every parameter the issue did not name must remain.
  Do not infer additional removals from a desired simplification. Use an empty signature_changes array when the issue
  names no parameter removal.
- Do not invent paths, symbols, signatures, visibility, or underlying types. Ground details in the supplied inputs.
  Reference suggestions are not observed repository facts; apply the reference reconciliation rules when provided.
- Return the JSON object immediately with no prose or Markdown fences.
Output JSON matching this schema exactly:
{json.dumps({key: value for key, value in _EXTRACTION_RESPONSE_SCHEMA.items() if key != "title"}, ensure_ascii=True, separators=(",", ":"))}
"""
_REFERENCE_RULES: Final[str] = """\
Reference-led contract synthesis: give the supplied <task_reference_context> deliberate priority
when interpreting and decomposing the task. It is substantive task guidance for this extraction,
not an optional afterthought to an issue-only list. Complete the following reconciliation before
returning the JSON contract; do not output the intermediate analysis:
1. Read the entire reference first. Identify each material clarification of expected behavior,
   edge cases, public entry points, compatibility, invariants and integration conditions.
2. Map each such point to the corresponding issue obligation and observed repository context.
   Incorporate every compatible clarification into the actual G/R/K wording. It need not appear
   verbatim in the issue: a concrete interpretation of requested behavior should not be discarded
   merely because the issue is terse. Keep the issue's full obligation alongside its clarification.
3. Put observable behavior and boundary cases in R, preservation and integration conditions in K,
   and use relevant clarifications to make G precise. Keep independent obligations separate.
   Preserve the operation, conditions, expected result, negation and exceptions; a generic label,
   file name or reminder to consult the reference does not account for a concrete guidance point.
4. Reconcile before promoting guidance into an obligation. The original issue remains authoritative.
   Do not expand its task scope, narrow explicit behavior, or replace its instructions. Repository
   facts require observed repository evidence. Keep a guidance-derived integration condition
   conditional when exploration has not established its premise. Suggested algorithms, file edits
   and test tactics are options, not mandatory implementation choices; retain the underlying
   behavioral or preservation obligation instead. Omit incompatible or unrelated advice.
5. Check that every material compatible reference point is represented in the contract and that
   no original issue obligation was lost. Deduplicate equivalent points without erasing conditions.
   Do not invent a target count or quota of reference-derived requirements.
6. Mark materially clarified R/K clauses reference_derived=true. This marks provenance, not truth,
   successful implementation, test coverage or evidence strength. All obligations still need checks.
This reference is private extraction input only. Return the derived contract in the required schema,
not a copy of the reference, its notice, document identity or a description of this synthesis process.
Ordinary Claims and Evidence must still come from actual execution.
"""
_REPOSITORY_RULES: Final[str] = """
A <repository_context> block may follow the issue: tool output the coding agent observed while exploring the
repository before editing. Details may also come from it. Use it to ground the contract, never to add behavior the
issue does not ask for:
- After a requirement's complete issue wording, append " [where: ...]" naming the existing files, symbols or entry
  points from the context that the requirement touches. Name only what appears in the context.
- Add K* constraints for existing behavior the change must keep working and for project conventions the new code
  must follow, as observed in the context. Prefix each with "(repo) " and cite the file or symbol.
- When the context is silent about a clause, keep the clause as the issue states it.
"""


@dataclass(frozen=True, slots=True)
class ExtractedRequirement:
    """One complete behavioral requirement returned by the setup side call."""

    text: str
    reference_derived: bool = False


@dataclass(frozen=True, slots=True)
class ExtractedConstraint:
    """One complete constraint returned by the setup side call."""

    text: str
    reference_derived: bool = False


@dataclass(frozen=True, slots=True)
class ExtractedSignatureChange:
    """A conservative parameter-removal contract stated by the issue."""

    symbol: str
    params_named_for_removal: tuple[str, ...]
    unnamed_params_policy: Literal["preserve"] = "preserve"


@dataclass(frozen=True, slots=True)
class IssueExtraction:
    """Typed result of the one-shot issue extraction side call."""

    requirements: tuple[ExtractedRequirement, ...] = ()
    constraints: tuple[ExtractedConstraint, ...] = ()
    signature_changes: tuple[ExtractedSignatureChange, ...] = ()
    goal: str = ""


# ── Middleware ───────────────────────────────────────────────────────


# ── Issue layer generation ───────────────────────────────────────────


def generate_issue_layer(
    issue_text: str,
    task_id: str = "",
    *,
    extraction: IssueExtraction | None = None,
    extraction_fallback: bool = False,
) -> GoalState:
    """Extract the issue title, objective, symptoms, reproduction, and criteria."""
    issue_text = _issue_contract_text(issue_text)
    title = _extract_title(issue_text)
    objective = _extract_summary_objective(issue_text) or _extract_objective(issue_text)
    if extraction is not None and extraction.goal:
        objective = extraction.goal
    elif re.search(r"(?mi)^\s*(?:#{1,6}\s*)?(?:task|任务|任务描述)\s*[:：]", issue_text):  # noqa: RUF001
        objective = title
    symptom = _extract_symptom(issue_text)
    reproduction = _extract_reproduction(issue_text)
    if extraction is not None:
        extraction = _reconcile_issue_extraction(issue_text, extraction)
        requirements = extraction.requirements
        constraints = extraction.constraints
    else:
        explicit_constraints = tuple(ExtractedConstraint(item) for item in _extract_explicit_constraints(issue_text))
        fallback_items = _extract_requirements(issue_text)
        requirements = tuple(
            ExtractedRequirement(item) for item in fallback_items if _fallback_requirement_kind(item) == "behavior"
        )
        constraints = _merge_constraints(
            tuple(
                ExtractedConstraint(item) for item in fallback_items if _fallback_requirement_kind(item) == "constraint"
            ),
            explicit_constraints,
        )

    instance_id = task_id
    prefix = f"[{instance_id}] " if instance_id else ""
    description_text = (extraction.goal if extraction is not None else "") or title or objective
    description = (
        _truncate(f"{prefix}{description_text}", MAX_GOAL_TEXT_LENGTH)
        if description_text
        else "Issue resolution in progress"
    )

    conclusions: list[GoalConclusion] = []
    if objective and objective.strip() != (title or "").strip():
        conclusions.append(
            GoalConclusion(
                content=_truncate(objective, MAX_CONCLUSION_TEXT_LENGTH),
                tag=TAG_OBJECTIVE,
            )
        )
    if symptom:
        conclusions.append(
            GoalConclusion(
                content=_truncate(symptom, MAX_CONCLUSION_TEXT_LENGTH),
                tag=TAG_SYMPTOM,
            )
        )
    if reproduction:
        conclusions.append(
            GoalConclusion(
                content=_truncate(reproduction, MAX_CONCLUSION_TEXT_LENGTH),
                tag=TAG_REPRODUCTION,
            )
        )
    conclusions.extend(
        GoalConclusion(
            content=requirement.text,
            tag=TAG_CRITERIA,
            reference_derived=requirement.reference_derived,
        )
        for requirement in requirements
    )
    conclusions.extend(
        GoalConclusion(
            content=constraint.text,
            tag=TAG_CONSTRAINT,
            reference_derived=constraint.reference_derived,
        )
        for constraint in constraints
    )
    if extraction is not None:
        conclusions.extend(
            GoalConclusion(
                content=_signature_change_content(item),
                tag=TAG_CONSTRAINT,
            )
            for item in extraction.signature_changes
        )
    elif extraction_fallback:
        conclusions.append(GoalConclusion(content="extraction=regex_fallback", tag=TAG_EXTRACTION))

    return GoalState(description=description, conclusions=tuple(conclusions))


def parse_issue_extraction(text: str) -> IssueExtraction:
    """Strictly decode the setup side-call JSON without silently dropping entries."""
    try:
        payload = json.loads(text)
    except ValueError:
        raise ModelCallError("invalid_json") from None
    fields = {"requirements", "constraints", "signature_changes"}
    if not isinstance(payload, dict) or set(payload) not in (fields, fields | {"goal"}):
        raise ResponseSchemaError(
            "response", "object_fields", expected_fields=fields | {"goal"},
            extra_fields=set(payload) - (fields | {"goal"}) if isinstance(payload, dict) else (),
        )
    requirement_entries = _extraction_object_list(payload["requirements"], "requirements")
    constraint_entries = _extraction_object_list(payload["constraints"], "constraints")
    signature_change_entries = _extraction_object_list(payload["signature_changes"], "signature_changes")
    requirements: list[ExtractedRequirement] = []
    for index, entry in enumerate(requirement_entries, start=1):
        if set(entry) not in ({"id", "text"}, {"id", "text", "reference_derived"}) or entry.get("id") != f"R{index}":
            raise ResponseSchemaError(f"requirements[{index - 1}]", f"fields=id,text; id=R{index}")
        requirements.append(
            ExtractedRequirement(
                text=_extraction_string(entry.get("text"), "requirements.text"),
                reference_derived=_reference_flag(entry),
            )
        )
    constraints: list[ExtractedConstraint] = []
    for index, entry in enumerate(constraint_entries, start=1):
        if set(entry) not in ({"id", "text"}, {"id", "text", "reference_derived"}) or entry.get("id") != f"K{index}":
            raise ResponseSchemaError(f"constraints[{index - 1}]", f"fields=id,text; id=K{index}")
        constraints.append(
            ExtractedConstraint(
                text=_extraction_string(entry.get("text"), "constraints.text"),
                reference_derived=_reference_flag(entry),
            )
        )
    signature_changes: list[ExtractedSignatureChange] = []
    signature_change_fields = {"symbol", "params_named_for_removal", "unnamed_params_policy"}
    for entry in signature_change_entries:
        if set(entry) != signature_change_fields:
            raise ValueError("issue extraction signature change fields are invalid")
        policy = _extraction_string(entry.get("unnamed_params_policy"), "signature_changes.unnamed_params_policy")
        if policy != "preserve":
            raise ValueError(f"unknown unnamed parameter policy: {policy}")
        params = _extraction_string_tuple(
            entry.get("params_named_for_removal"),
            "signature_changes.params_named_for_removal",
        )
        if not params:
            raise ValueError("signature_changes.params_named_for_removal must not be empty")
        signature_changes.append(
            ExtractedSignatureChange(
                symbol=_extraction_string(entry.get("symbol"), "signature_changes.symbol"),
                params_named_for_removal=params,
            )
        )
    return IssueExtraction(
        requirements=tuple(requirements),
        constraints=tuple(constraints),
        signature_changes=tuple(signature_changes),
        goal=_extraction_string(payload["goal"], "goal") if "goal" in payload else "",
    )


def _extraction_object_list(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be an array of objects")
    return value  # type: ignore[return-value]


def _extraction_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _reference_flag(entry: dict) -> bool:
    value = entry.get("reference_derived", False)
    if not isinstance(value, bool):
        raise ResponseSchemaError("reference_derived", "must be a boolean")
    return value


def _extraction_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be an array of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _signature_change_content(item: ExtractedSignatureChange) -> str:
    removed = ", ".join(item.params_named_for_removal)
    return f"{item.symbol} signature change: remove only [{removed}]; all other parameters preserved"


_URL_ONLY_RE = re.compile(
    r"^(?:tackles|fixes|closes|refs?|see|related)?\s*https?://\S+$",
    re.IGNORECASE,
)
_REQUIREMENTS_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:requirements?|acceptance criteria)\s*:?[ \t]*$",
    re.IGNORECASE,
)
_EXPLICIT_CONSTRAINT_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:constraints?|restrictions?|compatibility(?: requirements?)?|non-goals?)\s*:?[ \t]*$",
    re.IGNORECASE,
)
_NAMED_DELIVERABLES_LABEL_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?new interfaces introduced\s*:?\s*(.*?)\s*$",
    re.IGNORECASE,
)
_NAMED_DELIVERABLE_RE = re.compile(r"^\s*Name\s*:\s*(.+?)\s*$", re.IGNORECASE)
_NAMED_DELIVERABLE_FIELD_RE = re.compile(
    r"^\s*(Type|Path|Location|Inputs?|Outputs?|Description|Signature|Visibility|Underlying)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_TITLE_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*)?title\s*:?\s*$", re.IGNORECASE)
_SUMMARY_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*)?summary\s*:?[ \t]*(.*)$", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:-(?!-)[ \t]*|\*[ \t]+|\d+[.)][ \t]+)(.+?)\s*$")
_INLINE_LIST_ITEM_RE = re.compile(r"(?<=[.!?。！？])[ \t]+[-*][ \t]+(?=\S)")  # noqa: RUF001
_FALLBACK_REQUIREMENT_RE = re.compile(
    r"(?:"
    r"\b(?:must|should|cannot|can't|never|preserve|remain|keep|without)\b|"
    r"\bdo\s+not\b|\bdon't\b|"
    r"(?:需要|必须|不得|不许|不能|禁止|不可|不要|保持)"
    r")",
    re.IGNORECASE,
)
_CONTRACT_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"^\s*(?:do\s+not|don't|never|avoid|preserve)\b|"
    r"\b(?:must|should)\s+not\b|"
    r"\b(?:must|should)\s+continue\b|"
    r"\b(?:must|should|may)\s+only\b|"
    r"\b(?:avoid|unless)\b|"
    r"\bno\s+new\b|"
    r"\b(?:cannot|can't)\s+be\s+(?:changed|modified|removed|added)\b|"
    r"\bwithout\s+(?:changing|modifying|removing|adding)\b|"
    r"\bpreserv(?:e|es|ed|ing)\b|"
    r"\bremain(?:s|ed|ing)?\b|"
    r"\bstay(?:s|ed|ing)?\s+unchanged\b|"
    r"\bkeep(?:s|ing)?\s+(?:the\s+)?existing\b|"
    r"(?:不得|不许|不能|禁止|不可|不要|保持|不(?:改变|更改|修改|移除|删除|新增|增加|减少))"
    r")",
    re.IGNORECASE,
)
_PROSE_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"^\s*(?:do\s+not|don't|never|avoid|preserve)\b|"
    r"^\s*keep(?:s|ing)?\s+(?:the\s+)?existing\b|"
    r"\b(?:must|should)\s+not\b|"
    r"\b(?:must|should|may)\s+only\b|"
    r"\b(?:must|should)\s+continue\b|"
    r"\bno\s+new\b|"
    r"\b(?:cannot|can't)\s+be\s+(?:changed|modified|removed|added)\b|"
    r"\bwithout\s+(?:changing|modifying|removing|adding)\b|"
    r"\b(?:must|should)\b[^.!?。！？\n]{0,160}\bpreserv(?:e|es|ed|ing)\b|"  # noqa: RUF001
    r"\bremain(?:s|ed|ing)?\b|"
    r"\bstay(?:s|ed|ing)?\s+unchanged\b|"
    r"\bkeep(?:s|ing)?\s+(?:the\s+)?existing\b|"
    r"(?:不得|不许|不能|禁止|不可|不要|保持|不(?:改变|更改|修改|移除|删除|新增|增加|减少))"
    r")",
    re.IGNORECASE,
)


def _extract_title(issue_text: str) -> str | None:
    lines = issue_text.splitlines()
    # Explicit task identity wins even when workflow restrictions precede it.
    # Only fall back to the first prose line after searching the whole message.
    for index, line in enumerate(lines):
        clean = line.strip()
        if _TITLE_HEADING_RE.match(clean) is not None:
            return _first_line("\n".join(lines[index + 1 :]), max_length=MAX_GOAL_TEXT_LENGTH)
        explicit = re.match(r"^(?:#{1,6}\s*)?title\s*:\s*(.+)$", clean, re.IGNORECASE)
        if explicit:
            return _truncate(explicit.group(1).strip(), MAX_GOAL_TEXT_LENGTH)
    # Explicit task sections take precedence over an execution preamble.
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:#{1,6}\s*)?(?:task|任务|任务描述)\s*[:：]\s*(.*)$", line, re.IGNORECASE)  # noqa: RUF001
        if match:
            return _first_line(match.group(1) or "\n".join(lines[index + 1 :]), max_length=MAX_GOAL_TEXT_LENGTH)
    for index, line in enumerate(lines):
        clean = line.strip()
        if not clean:
            continue
        if _TITLE_HEADING_RE.match(clean) is not None:
            return _first_line("\n".join(lines[index + 1 :]), max_length=MAX_GOAL_TEXT_LENGTH)
        title_match = re.match(r"^#{1,6}\s*(?:title\s*:\s*)?(.+)$", clean, re.IGNORECASE)
        if title_match is not None:
            return _truncate(title_match.group(1).strip(), MAX_GOAL_TEXT_LENGTH)
        explicit_match = re.match(r"^title\s*:\s*(.+)$", clean, re.IGNORECASE)
        if explicit_match is not None:
            return _truncate(explicit_match.group(1).strip(), MAX_GOAL_TEXT_LENGTH)
        if _SUMMARY_HEADING_RE.match(clean) is not None or _REQUIREMENTS_HEADING_RE.match(clean) is not None:
            continue
        if not clean.startswith(("<", "```")):
            return _truncate(clean, MAX_GOAL_TEXT_LENGTH)
    return None


def _issue_contract_text(issue_text: str) -> str:
    return issue_text.strip()


def _extract_summary_objective(issue_text: str) -> str | None:
    lines = issue_text.splitlines()
    for index, line in enumerate(lines):
        match = _SUMMARY_HEADING_RE.match(line)
        if match is None:
            continue
        inline = match.group(1).strip()
        if inline:
            return _truncate(inline, MAX_CONCLUSION_TEXT_LENGTH)
        return _first_line("\n".join(lines[index + 1 :]), max_length=MAX_CONCLUSION_TEXT_LENGTH)
    return None


def _extract_requirements(issue_text: str) -> list[str]:
    """Extract bounded explicit issue requirements, with a predicate fallback."""
    listed = _extract_requirement_list_items(issue_text)
    if listed:
        return listed

    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", issue_text)  # noqa: RUF001
    result = []
    for sentence in sentences:
        clean = sentence.strip().lstrip("-* ")
        if clean and _FALLBACK_REQUIREMENT_RE.search(clean):
            result.append(clean)
        if len(result) >= 8:
            break
    return result


def _extract_requirement_list_items(issue_text: str) -> list[str]:
    """Return complete list items from the issue's explicit Requirements section."""
    lines = issue_text.splitlines()
    for index, line in enumerate(lines):
        if _REQUIREMENTS_HEADING_RE.match(line) is None:
            continue
        result: list[str] = []
        saw_list = False
        for candidate in lines[index + 1 :]:
            items = _split_list_items(candidate)
            if items:
                saw_list = True
                result.extend(items[: 16 - len(result)])
                if len(result) >= 16:
                    break
                continue
            stripped = candidate.strip()
            if stripped.startswith("#") or (saw_list and stripped):
                break
        if result:
            return result
    return []


def _split_list_items(candidate: str) -> tuple[str, ...]:
    """Parse one list line and split only punctuation-delimited inline bullets."""
    match = _LIST_ITEM_RE.match(candidate)
    if match is None:
        return ()
    return tuple(part.strip() for part in _INLINE_LIST_ITEM_RE.split(match.group(1)) if part.strip())


def _fallback_requirement_kind(text: str) -> str:
    """Classify only obvious prohibitions and invariants as constraints."""
    return "constraint" if _CONTRACT_CONSTRAINT_RE.search(text) is not None else "behavior"


def _extract_explicit_constraints(issue_text: str) -> list[str]:
    """Recover unambiguous constraints even when structured extraction misclassifies them."""
    result: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        list_match = _LIST_ITEM_RE.match(candidate)
        clean = list_match.group(1) if list_match is not None else candidate
        clean = clean.strip()
        if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {'"', "'"}:
            clean = clean[1:-1].strip()
        if not clean or clean.startswith("```"):
            return
        key = _constraint_key(clean)
        if key and key not in seen:
            seen.add(key)
            result.append(clean)

    listed_requirements = _extract_requirement_list_items(issue_text)
    for requirement in listed_requirements:
        if _CONTRACT_CONSTRAINT_RE.search(requirement) is not None:
            add(requirement)

    lines = issue_text.splitlines()
    in_constraint_section = False
    for index, line in enumerate(lines):
        label_match = _NAMED_DELIVERABLES_LABEL_RE.match(line)
        if label_match is not None:
            inline = label_match.group(1).strip()
            if inline:
                add(inline)
            else:
                deliverables = _named_deliverable_constraints(lines, index + 1)
                if deliverables:
                    for deliverable in deliverables:
                        add(deliverable)
                else:
                    for following in lines[index + 1 :]:
                        if not following.strip():
                            continue
                        if re.match(r"^\s*#{1,6}\s+", following):
                            break
                        add(following)
                        break
            in_constraint_section = False
            continue
        if _EXPLICIT_CONSTRAINT_HEADING_RE.match(line) is not None:
            in_constraint_section = True
            continue
        if re.match(r"^\s*#{1,6}\s+", line):
            in_constraint_section = False
            continue
        if in_constraint_section and line.strip():
            add(line)

    if not listed_requirements:
        sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", issue_text)  # noqa: RUF001
        for sentence in sentences:
            clean = sentence.strip().lstrip("-* ")
            if (
                clean
                and _NAMED_DELIVERABLES_LABEL_RE.match(clean) is None
                and _PROSE_CONSTRAINT_RE.search(clean) is not None
            ):
                add(clean)
    return result


def _named_deliverable_constraints(lines: list[str], start: int) -> tuple[str, ...]:
    """Fold each issue-declared Name block into one complete textual constraint."""
    result: list[str] = []
    fields: list[str] = []
    for line in lines[start:]:
        clean = line.strip()
        if not clean:
            continue
        if clean.startswith("</") or re.match(r"^#{1,6}\s+", clean):
            break
        name_match = _NAMED_DELIVERABLE_RE.match(clean)
        if name_match is not None:
            if fields:
                result.append(" · ".join(fields))
            fields = [f"Name: {name_match.group(1).strip()}"]
            continue
        field_match = _NAMED_DELIVERABLE_FIELD_RE.match(clean)
        if field_match is not None and fields:
            label = field_match.group(1)
            value = field_match.group(2).strip()
            fields.append(f"{label}: {value}")
            continue
        if fields:
            fields[-1] = f"{fields[-1]} {clean}"
    if fields:
        result.append(" · ".join(fields))
    return tuple(result)


def _merge_constraints(
    primary: tuple[ExtractedConstraint, ...],
    supplemental: tuple[ExtractedConstraint, ...],
) -> tuple[ExtractedConstraint, ...]:
    result: list[ExtractedConstraint] = []
    seen: set[str] = set()

    def add(item: ExtractedConstraint) -> None:
        key = _constraint_key(item.text)
        if key and key not in seen:
            seen.add(key)
            result.append(item)

    for item in primary:
        add(item)
    for item in supplemental:
        identity = _named_deliverable_identity(item.text)
        if identity is not None and any(
            _constraint_mentions_named_deliverable(existing.text, identity) for existing in result
        ):
            continue
        add(item)
    return tuple(result)


def _named_deliverable_identity(text: str) -> tuple[str, str, str] | None:
    name_match = re.match(r"^Name:\s*(.+?)\s+·\s+Type:\s*(.+?)\s+·", text, re.IGNORECASE)
    path_match = re.search(r"(?:^|\s+·\s+)(?:Path|Location):\s*(.+?)(?:\s+·\s+|$)", text, re.IGNORECASE)
    if name_match is None or path_match is None:
        return None
    name = name_match.group(1).strip().strip("`")
    kind = name_match.group(2).strip().strip("`")
    path = path_match.group(1).strip().strip("`")
    return (name, kind, path) if name and kind and path else None


def _constraint_mentions_named_deliverable(text: str, identity: tuple[str, str, str]) -> bool:
    name, kind, path = identity
    if not _contains_contract_token(text, path):
        return False
    text_without_path = re.sub(re.escape(path), "", text, flags=re.IGNORECASE)
    return all(_contains_contract_token(text_without_path, token) for token in (name, kind))


def _contains_contract_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def _reconcile_issue_extraction(issue_text: str, extraction: IssueExtraction) -> IssueExtraction:
    """Correct unambiguous R/K misclassification using issue-owned text."""
    supplemental = tuple(ExtractedConstraint(item) for item in _extract_explicit_constraints(issue_text))
    # Recover verbatim behavioral list items from an explicit Requirements
    # section even if a structured reply placed them in K or omitted them.
    behaviors = tuple(
        item for item in _extract_requirement_list_items(issue_text) if _fallback_requirement_kind(item) == "behavior"
    )
    behavior_keys = {_constraint_key(item) for item in behaviors}
    constraints = tuple(
        item
        for item in _merge_constraints(extraction.constraints, supplemental)
        if _constraint_key(item.text) not in behavior_keys
    )
    constraint_keys = {_constraint_key(item.text) for item in constraints}
    requirements = [item for item in extraction.requirements if _constraint_key(item.text) not in constraint_keys]
    known = {_constraint_key(item.text) for item in requirements}
    requirements.extend(ExtractedRequirement(item) for item in behaviors if _constraint_key(item) not in known)
    return IssueExtraction(
        requirements=tuple(requirements),
        constraints=constraints,
        signature_changes=extraction.signature_changes,
        goal=extraction.goal,
    )


def _constraint_key(text: str) -> str:
    normalized = " ".join(text.split()).strip("`\"' ").rstrip(".。")
    return normalized.casefold()


def _extract_objective(issue_text: str) -> str | None:
    for line in issue_text.split("\n"):
        line = line.strip()
        if not line or line.startswith(("#", "<", "```")):
            continue
        if line.lower().startswith("description"):
            continue
        if _URL_ONLY_RE.match(line):
            continue
        if len(line) > 10:
            return line
    return None


def _extract_symptom(issue_text: str) -> str | None:
    match = _ACTUAL_RE.search(issue_text)
    if match:
        text = match.group(1).strip()
        if text:
            return _first_line(text)
    match = _EXPECTED_RE.search(issue_text)
    if match:
        text = match.group(1).strip()
        if text:
            return f"Expected: {_first_line(text)}"
    return None


def _extract_reproduction(issue_text: str) -> str | None:
    match = _REPRO_RE.search(issue_text)
    if not match:
        return None
    text = match.group(1).strip()
    lines = [part.strip() for part in text.split("\n") if part.strip()][:3]
    return " → ".join(lines) if lines else None


# ── Goal injection format ────────────────────────────────────────────


def _format_goal_for_injection(state: GoalState) -> str | None:
    goal_attrs = [c for c in state.conclusions if c.tag in _ALL_GOAL_TAGS]
    agent_obs = [c for c in state.conclusions if c.tag not in _ALL_GOAL_TAGS and c.tag != TAG_PROGRESS]
    if not goal_attrs and not agent_obs and not state.description:
        return None

    lines = ["<goal_context>", f"Goal: {state.description}", ""]

    for title, tags in (
        ("From issue report:", _ISSUE_TAGS),
        ("From analysis:", (TAG_ROOT_CAUSE, TAG_SCOPE)),
        ("From planning:", (TAG_FIX,)),
        ("From implementation:", (TAG_VERIFICATION,)),
    ):
        items = [c for c in goal_attrs if c.tag in tags]
        if items:
            lines.append(title)
            for c in items:
                mark = _STATUS_MARKS.get(c.status, "?")
                lines.append(f"  [{mark}] {_TAG_LABELS[c.tag]}: {c.content}")

    # Include agent's own observations (untagged conclusions from goal_write).
    # These are the LLM's additional findings that don't map to a structural
    # tag but may carry useful context for the next phase.
    if agent_obs:
        lines.append("Agent observations:")
        for c in agent_obs:
            mark = _STATUS_MARKS.get(c.status, "?")
            lines.append(f"  [{mark}] {c.content}")

    lines.append("")
    lines.append(
        "Keep these objectives in mind. Your work should address the pending items and build on the confirmed findings."
    )
    lines.append("</goal_context>")
    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────


def _first_line(text: str, *, max_length: int = 300) -> str | None:
    for line in text.split("\n"):
        line = line.strip()
        if line and len(line) > 5:
            return _truncate(line, max_length)
    return None


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"


async def extract_issue_structure(
    model: MaintenanceModel,
    issue_text: str,
    *,
    timeout_seconds: float,
    call_settings: CallSettings | None = None,
    repository_context: str = "",
    reference_context: str = "",
    deadline: float | None = None,
) -> IssueExtraction:
    """Run the one-shot stateless setup extraction and return a typed result.

    The port owns provider wiring and the timeout. Everything this step needs
    to be correct is here: the schema, the rules, and the reconciliation that
    puts back any named deliverable the model dropped. ``repository_context``
    is what exploration observed; the reconciliation still checks the issue only.
    """
    issue_text = _issue_contract_text(issue_text)
    settings = call_settings or replace(CallSettings(), timeout_seconds=timeout_seconds)
    context = f"\n\n<repository_context>\n{repository_context}\n</repository_context>" if repository_context else ""
    reference = f"\n\n<task_reference_context>\n{reference_context}\n</task_reference_context>" if reference_context else ""
    return await structured_call(
        model,
        system_prompt=(_REFERENCE_RULES if reference else "") + _EXTRACTION_SYSTEM_RULES
        + (_REPOSITORY_RULES if context else ""),
        user_prompt=(reference.lstrip() + f"\n\n<original_issue>\n{issue_text}\n</original_issue>" + context
                     if reference else issue_text + context),
        response_schema=deepcopy(_EXTRACTION_RESPONSE_SCHEMA),
        settings=settings,
        parse=lambda reply: _checked_issue_extraction(issue_text, reply.text or "", reference_present=bool(reference)),
        deadline=deadline,
    )


def _checked_issue_extraction(issue_text: str, text: str, *, reference_present: bool = False) -> IssueExtraction:
    extraction = _reconcile_issue_extraction(issue_text, parse_issue_extraction(text))
    if not reference_present:
        extraction = replace(extraction,
            requirements=tuple(replace(item, reference_derived=False) for item in extraction.requirements),
            constraints=tuple(replace(item, reference_derived=False) for item in extraction.constraints))
    # A bounded semantic guard, not a second model evaluator. Only an explicit
    # behavior/acceptance section with positive obligations requires an R entry.
    # Constraint-only tasks and named deliverables remain valid without R entries.
    sections = re.split(r"(?m)^\s*#{1,6}\s+", issue_text)
    behavioral = any(
        re.match(
            r"(?:expected\s+(?:behavior|result|output)|requirements?|acceptance criteria)\s*:?\s*\n",
            section,
            re.IGNORECASE,
        )
        and any(_fallback_requirement_kind(item) == "behavior" for item in _extract_requirements(section))
        for section in sections
    )
    if behavioral and not extraction.requirements:
        raise ModelCallError("missing_behavioral_requirements")
    return extraction


def requirement_entries(goal: GoalState) -> tuple[tuple[str, str], ...]:
    """Return the public R/K contract in stable Goal order."""
    requirements = tuple(
        (f"R{index}", conclusion.content)
        for index, conclusion in enumerate(
            (item for item in goal.conclusions if item.tag == "criteria"),
            start=1,
        )
    )
    constraints = tuple(
        (f"K{index}", conclusion.content)
        for index, conclusion in enumerate(
            (item for item in goal.conclusions if item.tag == "constraint"),
            start=1,
        )
    )
    return requirements + constraints
