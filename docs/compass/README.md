# Compass layer for iCode

2026-11-20 最新入口与迁移说明见 [Extension 接入审核](EXTENSION_MIGRATION_REVIEW.md)。下方早期审核文档保留历史记录。

提交前四项宿主规范修复及实际无界面验证，见 [提交审核](SUBMISSION_REVIEW.md)。

最新修订：界面取消总时间上限、保留 250 次主请求；修复原生执行接口兼容性。当前验证和历史记录的区别见 [宿主回归修复审核](HOST_REGRESSION_REVIEW.md)。

Latest adapter readability update: named Brief/flag records, type annotations and 9 new checks. See [readability review](READABILITY_REVIEW.md); the task workflow is unchanged.

Compass is an independent capability layer under `src/compass/`, alongside iCode's `src/chrys/`. The iCode adapter supports both an opt-in native main session and the isolated headless evaluation entry. Compass is off by default. In the iCode UI, open `/agents tools`, enable **compass** under **Extensions**, and save. Run `uv sync --locked` after updating this checkout so its `chrys.extensions` entry point is installed. The native session keeps the selected model and approval settings; see [UI review](TUI_REVIEW.md).

An active UI task now survives user turns: follow-ups keep its requirements, evidence and budget. Use `/new` for a different task. Requirement changes use the UI-only `update_compass_requirements` tool. See the [multi-turn review](MULTITURN_REVIEW.md) for examples and the 22 new offline tests.

## Layout

```text
src/compass/
├── core/                  # Frozen goals, evidence, claims and Brief policy (unchanged)
├── ports.py               # Model, event and artifact-store interfaces (unchanged)
├── runtime/
│   ├── run_state.py       # RunState: budget, handoff, audit and core ownership
│   ├── workflow.py        # Strict headless workflow + shared completion/check policy
│   ├── records.py         # Requests, results, backend protocol and runtime errors
│   ├── budget.py          # 250 calls / 11,400 seconds, one definition
│   └── prompts.py         # Workflow, submission and closing/conflict reminders
├── adapters/icode/
│   ├── interactive.py     # Native task continuity, user updates, restore and retry adaptation
│   ├── headless.py        # Dedicated headless host adapter
│   ├── tools.py           # Extension tools + stable context + native before-tool guard
│   ├── scope.py           # Route tools to the current main invocation only
│   ├── migrate.py         # Explicit preview/apply for the old profile switch
│   ├── middleware.py      # Evidence/Brief delivery; shared edit policy + evaluation guard
│   ├── interactive_model.py # Maintenance via the selected native model profile
│   ├── openrouter.py      # Frozen headless OpenRouter model policy and transport
│   ├── credentials.py     # Owner-only key read and native command credential template
│   └── storage.py         # Native secure artifact persistence
├── provenance.py          # Source hash verification and manifest serialization helpers
├── provenance.json        # Original source identity and verification hashes (unchanged)
├── cli.py                 # Prepare isolated profiles and assemble the headless run
└── __main__.py            # python -m compass
```

The core and runtime do not import iCode or the OpenAI SDK. The entry point supplies iCode's secure artifact implementation to the session through the existing artifact port. The normal main-session extension registers tools, a stable context and a before-tool guard. Dynamic Brief delivery still attaches request middleware to the current host instance. The dedicated evaluation adapter retains its tool middleware and shares the same edit-policy function. It does not replace process-global host functions.

The separation follows the design of CodeHelix-RSI (`codex/codehelix-rsi-impl`, `84cff41a`), which is a reference, not a dependency.

## Isolated evaluation entry

Use the repository's locked environment:

```bash
uv sync --locked
uv run python -m compass --prepare-only --directory /absolute/path/new-preflight
```

The preflight verifies source provenance and prepares profiles; it reads no key and makes no model calls. Use a different new directory for the actual run:

```bash
uv run python -m compass \
  --task /absolute/path/public-task.txt \
  --cwd /absolute/path/task-repository \
  --directory /absolute/path/new-run \
  --key-file /absolute/path/openrouter.key
```

Run directories belong outside the task repository. The credential file must be an owner-only regular file. Profiles contain a native `{{!command}}` credential reference, not the key. The command uses isolated Python to recheck and read the original file at request time. Compass does not export the key to `os.environ`; maintenance receives it in memory. Preparation without `--key-file` leaves credentials blank and performs no credential command. File permissions do not isolate tools running as the same OS user: keep credentials outside task repositories and use an external sandbox when filesystem isolation is required. The dedicated profile is named `CompassCode`.

The wheel includes both `chrys` and `compass`. No separate `compass` console command is registered: `python -m compass` is the entry. The original `icode` console command remains unchanged.

## Preserved behavior

- Explore first, then call `start_compass_brief` alone with the exploration summary. Repository evidence must already exist.
- Use the original task extraction, claim/evidence maintenance, Brief rendering and submission prompts.
- Headless evaluation budget: 250 logical model invocations; main workflow deadline: 11,400 seconds. UI tasks keep the 250-attempt cap but have no Compass wall deadline. SDK HTTP retries remain separate from logical steps.
- Default main model: OpenRouter `deepseek/deepseek-v4.1-flash-1017`; DeepSeek only, no provider fallback, `reasoning.effort=max`, maximum output 64,000; SDK HTTP retries 2, host transient retries 0.
- Maintenance uses JSON-object responses, temperature 0 and reasoning disabled. Main temperature/top-p/top-k are not explicitly set.
- In the dedicated evaluation entry, and in a UI turn that attempted to start Compass or edit, check the existing Compass validity flags at completion. Run one submission-check continuation if at least 15 calls remain and the deadline has not expired.
- Keep the closing reminder after 200 calls, with real Brief delivery taking priority. Closing confirms only the reminder, never handoff or a Brief revision; other error/budget behavior, state persistence and cleanup are retained.
- Keep the existing public tool names and `compass_brief` persisted-state key for compatibility.

The historical source was labeled R5. That label is provenance, not the product/module name. `provenance.json` retains all 72 original file hashes and archive identity `62c304ae6315ffb1c977def04b77fb9c552ac75ceb533fd249c2b0180818d257`. This package uses 36 core/port modules; unused legacy host adapters and their packaging metadata are not shipped as runtime code. Core migration changes are import namespaces and formatting, plus one explicit I/O adjustment: `git status` receives `stdin=DEVNULL` rather than inheriting the parent input. Its arguments and output parsing are unchanged. The migration test pins this exact adjustment and compares all remaining core syntax against the original hashes. The two original submission prompt strings are checked separately, verbatim.

## Limits retained from the previous review

The main-session adapter uses public Extension tools, context, before-tool hooks and the session data directory, plus the pinned host's internal LoadedAgent boundary. The isolated entry supports a fresh headless session; the UI entry preserves active tasks across user turns and model rebuilds. Neither automatically propagates Compass to sub-agents or workflow nodes. The UI uses native approvals and tools, so it is not an identical benchmark environment. Use one process per isolated evaluation run.

This is not a Docker sandbox or an EvalHub verifier. Native tools execute in the caller's environment; the caller must supply container/network isolation for an evaluation. `completed` means the agent workflow completed, not verifier PASS.

The late-handoff issue is fixed: a closing reminder cannot advance Brief revision, conflict acknowledgements or the handoff flag. Interactive confirmation still requires a complete response; dedicated headless confirmation retains request-observation timing. These are observable local boundaries, not proof that a model understood the Brief.

Offline request tests verify serialized parameters, not live gateway enforcement. No paid model evaluation is performed by this migration. Local validation uses Python 3.14.0; the upstream 3.14.10 pin is unchanged and still needs exact-version validation.

In the UI, a turn that never starts/attempts Compass and never attempts an edit recognized by the existing guard may finish normally. It records `compass_skipped`, performs no maintenance or submission check, and is not labeled a valid Compass treatment. Failed/partial handoffs still fail. This is based on observed actions, not a natural-language classification or proof that arbitrary shell commands are read-only.

See [current fixes and review map](REVIEW_FIXES.md) for the latest changes and 26 new offline tests; [original review notes](REVIEW.md) retain historical validation.

Interactive native retries follow iCode's existing retry setting. Each re-entry into the Compass main-request middleware spends one of its 250 attempts, including failed attempts; SDK-internal HTTP retries are not separately counted. UI backoff does not consume a Compass wall deadline; the task still shares the 250-attempt cap. The UI adapter confirms Brief delivery only after a complete response and lets iCode replay failed-request injections. See [UI retry review](TUI_REVIEW.md#自动重试最新修订) for the boundaries and offline verification.

## Updating the frozen core

Startup verifies the 36 core/port file hashes and the two original submission prompt hashes. Renaming adapter/runtime modules does not require changing the frozen core manifest. A future core release (for example a different Brief version) must deliberately update its provenance and validation; do not disable verification to bypass a mismatch.

The UI has no Compass task wall deadline; idle time cannot expire a task. Native request timeouts and cancellation remain unchanged. Ordinary questions do not reset its submission-check latch; a later editing turn or requirement update does. Requirement-change tool selection remains model-driven: recorded follow-ups alone do not guarantee a rebuilt G/R/K contract. The headless workflow remains unchanged.

Normal-session artifacts now use `ToolContext.session_data_dir`. Restoring an older checkpoint preserves the ledger and copies only referenced raw evidence through the frozen core restore logic. Old artifact/audit directories remain untouched; conflicting target evidence is rejected. The checkpoint serializer bridge remains necessary.
