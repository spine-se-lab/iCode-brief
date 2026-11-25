# Compass layer for iCode

最新问题修复、真实复验和局限见 [需求分类与证据关联审核](CONTRACT_ASSESSMENT_REVIEW.md)。

最新宿主改动量见 [iCode 接线缩减审核](SMALL_HOST_SEAM_REVIEW.md)：builder 相对迁移 main 基线仅新增 7 行（只在启用 Compass 时导入），原版源码合计 4 个文件新增 39 行。

最新信息入口和接口缺口见 [Adapter 信息审核](ADAPTER_INFORMATION_REVIEW.md)。最新目录与调用链见 [扁平分层审核](FLAT_ARCHITECTURE_REVIEW.md)。最新持久化见 [独立持久化审核](PERSISTENCE_REVIEW.md)，原文捕获见 [原文捕获与探索后更新](CAPTURED_REQUIREMENTS_REVIEW.md)，整体设计见 [三工具接入审核](TOOL_INTERFACE_REVIEW.md)。该说明取代早期文档中的界面交接、编辑门禁、请求预算与收尾回合设计；历史文档保留供追溯。

Compass is opt-in. After `uv sync --locked`, enable **compass** under the agent's **Tools → Extensions** (`/agents tools`). It uses the selected model and native approvals/retries.

## Main-session tools

| Tool | Purpose |
| --- | --- |
| `inspect_compass_brief(refs)` | Query `goal`, requirement, constraint, claim or evidence IDs; `[]` returns the Brief. No model call. |
| `update_compass_requirements()` | Create or revise Goal/Requirements/Constraints from captured original user messages. Call initially and again after repository exploration; unchanged text and context skip extraction. |
| `update_compass_brief()` | Synchronize evidence, initialize missing Goal/Requirements from captured user text, then evaluate the core policy. Maintain only when due; otherwise return the current Brief. |

The main-build Extension context captures the original user text once per native turn; follow-ups are retained in order. Capture itself makes no model calls. The model calls the argument-free requirements tool initially and again after exploration.

Normal execution does not require a Compass handoff. There is no Compass main-request budget, edit gate, request middleware or extra submission pass in this main-session path. The host runner and bindings match the upstream base. Native limits still apply.

Context and public `after_tool` hooks enter `adapters/icode/information.py`. It records observed fields, origin, duration and disposition in the private session-local `adapter-input.jsonl`. The first hook queues original read/write/search/shell results; a second hook synchronizes them and checks the gate automatically. Below the maintenance threshold there is no model call. Due windows and fresh test results run maintenance, and the resulting Brief is appended to the original tool result for the next model request. Unmapped kinds, rejected calls and Compass feedback are audit-only. Shell `workdir` uses the native resolver; an absent exit-code suffix remains unknown. The automatic check records eligible observations as evidence; restored queues are processed on the next eligible tool result. Automatic checks use the same gate lock as explicit tools, so they do not concurrently process the same window. A failed window retains evidence and uses the existing cooldown before automatic retry; explicit retry remains available. Queries synchronize pending observations but do not extract requirements or maintain claims. Briefs enter the conversation as tool results. Automatic checks have an internal 240-second deadline inside the 250-second hook timeout; errors preserve the original tool output and add an unavailable notice, while user cancellation still propagates. This can delay a due tool result; it does not start background tasks or an extra agent pass. The hook-return log is not itself a provider delivery receipt.

The task ledger persists in Compass-owned `session.json` under the Extension session directory, beside `ledger/claims` and `ledger/evidence`. It no longer writes native history state or requires the host serializer. Legacy Compass `tool-state.json` files can be imported. Compass state that existed only in old native iCode history is no longer imported. Child invocations cannot access the main session's Compass tools; their ordinary tools remain unaffected.

## Human commands

`/compass` enters requirement update and then Brief maintenance checking, using the same gate policy as the tools. An unchanged contract or a maintenance threshold not yet reached does not force a model call. `/compass status` opens a read-only snapshot with grouped Goal, Requirements, Constraints, Claims, Tensions, Already read, Weak coverage and Evidence status sections. Opening it does not flush observations, write state or call a model. The native TUI registration is a small, explicitly approved bridge; the handlers and dialogs live in Compass. See [command review and manual checks](COMMANDS_REVIEW.md).

## Layout

```text
src/compass/
├── adapters/icode/            # Collect native inputs/results; deliver Brief as a tool result
│   ├── tools.py               # Three tools, observation and automatic maintenance hooks
│   ├── tool_session.py        # Native session lookup, resource ownership, gate calls
│   ├── commands.py            # Manual checks and immutable status snapshots
│   ├── tui/                   # Slash dispatch, maintenance and status dialogs
│   ├── information.py         # Unified context/tool input mapping and audit
│   ├── interactive_model.py   # Use the model selected in iCode
│   └── storage.py             # Implement the secure artifact port
├── gate/
│   ├── controller.py         # Decide requirement creation/update and Brief maintenance
│   └── maintenance.py        # Original trigger windows and maintenance policy
├── task_contract/            # Extract and store Goal / Requirements / Constraints
├── claims/                   # Maintain, merge and inspect claims
├── evidence/                 # Record observations and evidence
├── briefing/                 # Select and render Brief content
├── conversation/             # Record user/agent statements
├── model_calls/              # Business model-call policy
├── persistence/              # Artifact locations and independent session checkpoint
├── session.py / state.py     # Connect business modules and serialize their state
├── ports.py / events.py      # Host-independent interfaces and events
├── evaluation/               # Separate historical budget/handoff/submission harness
├── provenance.py / .json     # Source verification with migration records
└── cli.py / __main__.py      # Isolated evaluation entry only
```

Product flow: **iCode Extension → adapter → gate → peer business modules → gate → adapter tool result**.
The gate decides whether a task contract is missing or its captured input/context changed, then whether the original maintenance policy is due. There is no `core/` or `runtime/` wrapper directory. Product imports do not load `evaluation/`.


The builder still supplies the selected model, lifecycle, a main-build-only input capture callback and a read-only legacy-checkpoint migration bridge. New state is Compass-owned; the serializer additions have been withdrawn. This is not yet a zero-host-change integration, but it does not modify or wrap the runner.

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

## Separate evaluation behavior

The dedicated `python -m compass` entry retains its original exploration/handoff, budget, submission check and model settings. The host integration redesign concerns the discovered main-session Extension. Shared default rendering now uses complete Briefs; evaluation callers that explicitly supply finite character budgets retain bounded rendering. Do not treat a successful tool-only session as proof of a valid historical R5 evaluation treatment. The frozen reproduction checkout is unchanged.

## Preserved algorithms and validation

The earlier approved change in `session.py` deduplicates extraction by user text plus repository context; skipping also requires a complete Goal and Requirements. The initial layout migration preserved that adaptation and the other 35 recorded modules with namespace/import changes only; subsequent approved behavior changes are recorded individually in provenance.json. `provenance.json` keeps the original algorithm hashes and separate current-file hashes. The gate reuses `replay_unmaintained`/`preview`; trigger thresholds are unchanged; default size/count/phase Claim folding is disabled for complete Brief rendering.

The `core` key inside existing checkpoint JSON is retained for data compatibility; it does not denote a directory or a new architectural layer. Independent persistence remains under the Extension's session directory.

The initial layout migration ran `test_flat_gate.py` (13 offline cases). For the 2026-11-24 complete-display change, Smart Test ran all 256 Compass integration tests and 77 selected architecture checks, all passing. The seven new completeness regressions also passed after adding an unloaded-session preview assertion. No paid evaluation was run. Local Python remains 3.14.0; the upstream 3.14.10 pin is unchanged. Historical interactive tests still describe the superseded wrapper.

Latest adapter validation (2026-11-23): 24 new information-boundary tests passed; only that new test file ran. Gate and business policy are unchanged. See the adapter review for unavailable public interfaces and fields that are audit-only.

- [Pagination follow-up, request delivery, and Brief history](FOLLOWUP_DELIVERY_REVIEW.md) — original-session audit and offline regression checks.

`/compass` and `/compass status` are parallel top-level command suggestions; selecting either executes directly without a submenu.

- [历史故障与当前要求核查（2026-11-24）](CONTRACT_HISTORY_REVIEW.md)：四轮需求、策略 5 的上下文保留、六个审核反例及离线验证限制。

- [Automatic maintenance and tool-result feedback (2026-11-24)](AUTOMATIC_MAINTENANCE_REVIEW.md): fixes the natural-use run that queued 82 observations without a Brief call; offline request-level verification, not a live-model success claim.

## Complete Brief display

The default `brief_budget_chars=None` renders the complete active Brief, including full Goal/Requirements/Constraints, active Claim statements and references, all symptoms/conflicts and recorded read coverage. TUI rows wrap without the former two-line preview cap. Requirement extraction no longer clips text or limits fallback requirement counts. Default maintenance does not fold Claims to satisfy Brief size or active-count limits. Current previews are rendered from state, including unloaded sessions; saved historical snapshots are unchanged. Explicit finite rendering budgets retain the legacy evaluation path. Stored evidence excerpts and provider context/output limits are separate from display limits.

The status popup now uses native top tabs rather than accordion/sidebar sections. Module selection stays in the same popup; individual entries open detail popups. Compact Update Brief and Brief & History actions remain distinct. History uses Older/Newer buttons. All titles and generated detail labels are bold; literal content remains plain, indented and complete.
