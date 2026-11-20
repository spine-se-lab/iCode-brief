# Copyright (c) 2026 Chrys. All rights reserved.

"""Validate a session envelope before replacing any live tracker."""

from compass.core.claims.models import parse_claims
from compass.core.evidence.models import evidence_id, parse_evidence
from compass.core.task_contract.models import parse_goal_state

STATE_KEY = "compass_brief"


def decode_state(raw: object) -> tuple[object, object, object]:
    if raw is None:
        return None, None, None
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported Compass Brief session schema")
    goal, evidence, claims = (raw.get(key) for key in ("goal", "evidence", "claims"))
    if not isinstance(goal, dict) or not isinstance(evidence, list) or not isinstance(claims, dict):
        raise ValueError("invalid Compass Brief session payload")
    sequences = [item.get("sequence") if isinstance(item, dict) else None for item in evidence]
    if any(type(value) is not int or value < 1 for value in sequences):
        raise ValueError("invalid evidence sequence")
    if sequences != sorted(set(sequences)):
        raise ValueError("evidence sequence must be unique and ordered")
    raw_claims = claims.get("claims", [])
    if not isinstance(raw_claims, list):
        raise ValueError("invalid claim table")
    parsed_evidence = parse_evidence(evidence)
    snapshot = parse_claims(claims)
    parsed_goal = parse_goal_state(goal)
    if not isinstance(goal.get("conclusions", []), list) or len(parsed_goal.conclusions) != len(
        goal.get("conclusions", [])
    ):
        raise ValueError("invalid task contract")
    if len(parsed_evidence) != len(evidence) or len(snapshot.claims) != len(claims.get("claims", [])):
        raise ValueError("session contains invalid evidence or claims")
    refs = {evidence_id(item.sequence) for item in parsed_evidence}
    ids = {item.id for item in snapshot.claims}
    if len(ids) != len(raw_claims):
        raise ValueError("duplicate claim identifiers")
    for claim in raw_claims:
        for field, allowed in (("evidence_refs", refs), ("supporting_claim_ids", ids), ("conflicts_with", ids)):
            values = claim.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or value not in allowed for value in values
            ):
                raise ValueError("session contains invalid claim references")
    raw_state = claims.get("state", {})
    if not isinstance(raw_state, dict):
        raise ValueError("invalid claim state")
    active = raw_state.get("active_ids", [])
    if not isinstance(active, list) or any(not isinstance(value, str) or value not in ids for value in active):
        raise ValueError("session contains invalid active claims")
    if any(set(item.evidence_refs) - refs or set(item.supporting_claim_ids) - ids for item in snapshot.claims):
        raise ValueError("session contains dangling provenance")
    if snapshot.state.processed_evidence_seq > max((item.sequence for item in parsed_evidence), default=0):
        raise ValueError("session claim cursor exceeds evidence ledger")
    return goal, evidence, claims


def import_legacy(state: dict) -> dict:
    """Explicit conversion; the caller keeps the original session unchanged."""
    if STATE_KEY in state:
        raise ValueError("session already contains Compass Brief")
    envelope = {
        "schema_version": 1,
        "goal": state.get("chrys_goal", {}),
        "evidence": state.get("chrys_evidence", []),
        "claims": state.get("chrys_claims", {}),
    }
    decode_state(envelope)
    return {**state, STATE_KEY: envelope}
