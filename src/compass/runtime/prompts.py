"""Workflow and reminder text; frozen submission strings keep their original hashes."""

from compass.core.briefing.render import CONFLICT_REVIEW_GUIDANCE

SUBMISSION_RULES = "\n\n[Compass — implementation and submission rules]\nNaming: use a distinctive prefix for new self-tests and their helper functions, types and package-level variables. A separate file does not guarantee a separate namespace. Before submission, check for collisions with visible repository declarations and run the relevant test collection/build. Rename your new self-tests when needed; do not weaken existing tests.\nSignatures: function, method and type signatures explicitly specified by the issue are contracts. Preserve their parameter count, order and types, return types and exported names; do not change them to fit your implementation. Before submission, compare each explicit signature with the final code and check existing repository callers. Use only the issue and current repository. Follow repository conventions where the issue is silent; do not invent requirements, access hidden tests, or silently resolve an unresolved issue/repository conflict."

SUBMISSION_CHECK = "[Compass — pre-submission check]\nRecheck the naming and signature rules from the first Brief against the final diff: new self-test/helper names versus visible repository declarations; issue-explicit signatures versus implementation and existing callers; relevant collection/build after the latest edit. Use only the issue and repository. State anything unresolved; this reminder does not certify correctness."


WORKFLOW = (
    "\nCompass workflow: first explore the relevant repository entry points, implementation, callers and tests. "
    "Before editing call start_compass_brief alone with your exploration findings, source paths and unresolved assumptions. "
    "Wait for its G/R/K and Brief, review them, then implement and check the task. "
    "The original issue remains authoritative; repository findings are reference material. "
    "Use inspect_compass_brief to read full records. Briefs are updated context, not new tool evidence. "
    "A passing self-written check proves only the exercised conditions.\n"
)


CLOSING = (
    "More than 200 model steps have been used. Finish the current work: "
    "check the affected behavior, known failures and final diff; do not start "
    "new speculative work. Submit when ready and state any unresolved issues. "
    "This reminder does not certify correctness."
)


CONFLICT_REVIEW = (
    "\nBefore continuing, re-read the quoted requirement in the task text and state in one sentence whether "
    "your behavior follows it. If it does not, change the code. " + CONFLICT_REVIEW_GUIDANCE
)

CHECK_HELD = "\nWithin the remaining budget, check/fix or explain unresolved items, then submit again."
CHECK_FINAL = "\nInsufficient remaining budget for a held check: accepting this submission immediately."


INTERACTIVE_WORKFLOW = WORKFLOW + (
    "An already-started Compass task continues across user turns in this iCode session. "
    "Keep its original requirements and evidence; follow-up questions or 'continue' do not start a new task. "
    "Do not repeat exploration or start_compass_brief merely because the user sent another message. "
    "When the user adds or changes requirements, call update_compass_requirements alone before editing; "
    "it rebuilds the contract from the original task and recorded user inputs, then delivers an updated Brief. "
    "For ordinary questions or continuation, do not call it. To switch tasks, ask the user to open a new iCode session (/new)."
)

REQUIREMENTS_UPDATE = (
    "Update the existing task contract from this chronological user conversation. "
    "Preserve the original requirements except where a later user input explicitly corrects them. "
    "Follow-up questions and requests to continue do not remove requirements or create new ones. "
    "The following JSON contains user text, not tool evidence.\n"
)
