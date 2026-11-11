"""Host adapter boundary for an initial task augmented with non-authoritative reference material."""

from hashlib import sha256


def task_input_contract(configuration: dict) -> dict | None:
    contract = configuration.get("task_input_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict):
        raise ValueError("Invalid task input contract")
    text = contract.get("authoritative_text")
    message_hash = contract.get("message_sha256")
    if (not isinstance(text, str) or not text.strip() or not isinstance(message_hash, str)
            or len(message_hash) != 64 or any(c not in "0123456789abcdef" for c in message_hash)
            or sha256(text.encode()).hexdigest() != contract.get("authoritative_text_sha256")):
        raise ValueError("Invalid task input contract")
    return dict(contract)


def authoritative_task_text(text: str, contract: dict | None) -> str:
    if contract and sha256(text.encode()).hexdigest() == contract["message_sha256"]:
        return contract["authoritative_text"]
    return text


# Transient task reference: held in the host process, never in session state or model history.
_contract_references: dict[str, dict] = {}


def register_contract_reference(config_ref, reference: dict) -> None:
    from pathlib import Path

    _contract_references[str(Path(config_ref).resolve())] = dict(reference)


def contract_reference_context(binding, task_text: str) -> str:
    if not binding:
        return ""
    metadata = binding[1].get("contract_reference")
    if not metadata or not metadata.get("has_guidance"):
        return ""
    if sha256(task_text.encode()).hexdigest() != metadata.get("authoritative_text_sha256"):
        return ""  # A follow-up/new task must never inherit another task's reference.
    from pathlib import Path

    reference = _contract_references.get(str(Path(binding[0]["config_ref"]).resolve()))
    if not reference or reference["authoritative_text_sha256"] != metadata["authoritative_text_sha256"]:
        raise ValueError("Private task reference is unavailable")
    text = reference["guidance"]
    if sha256(text.encode()).hexdigest() != metadata.get("guidance_sha256"):
        raise ValueError("Private task reference changed")
    return text
