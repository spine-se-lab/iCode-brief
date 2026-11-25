# Automatic maintenance review — 2026-11-24

## Observed failure

In the stopped FastAPI session `911556e2-9854-49c9-af62-8a95e0359c49`, the agent called `update_compass_requirements` successfully at 16:07:34 UTC (trajectory lines 25/57). At the 16:39:11 UTC snapshot, 82 eligible tool observations were queued, the core evidence/claims were empty, and no Brief update tool had been called. The original issue did not request Compass use. This is a trigger integration gap, not an observed maintenance-model failure.

## Change and boundaries

The product Extension keeps its original audit/observation hook and adds an automatic check after eligible tool results. The host-independent gate flushes observations and evaluates the existing maintenance policy under its existing lock. Due windows initialize missing requirements, maintain claims and append the resulting Brief to the original tool output. Captured user scope changes refresh the contract; ordinary unchanged checks make no model call. Explicit updates and automatic checks share the lock and processed/attempt cursors. Failed windows keep evidence, with automatic retries respecting the existing cooldown.

A 240-second internal deadline expires before the 250-second host-hook deadline. Ordinary errors append a safe unavailable notice without replacing the original output; external cancellation is not swallowed. This is synchronous at due tool boundaries and can delay that result. Rejected calls, unknown tool kinds, Compass tools and child invocations do not run this automatic check. Status remains read-only. No background scheduler, edit gate, extra completion turn, independent evaluation change, core algorithm/provenance change, new dependency or host runner change is included.

The Extension can rewrite ordinary returned tool results; the host does not apply replacements to a tool that raises directly. A `Compass automatic Brief returned` log records a hook return, not a provider receipt. The regression test captures the next native model request to prove delivery for normal returned results. A task with no eligible tool calls has no automatic tool-boundary check.

## Validation

The native-request reproduction failed before the fix: five ordinary tools left the gate session uninitialized. It now proves maintenance at the fourth observation, no repeat on the fifth, original output preservation, Brief presence in the next model request, and no Compass feedback in repository evidence. Additional offline cases cover fresh tests, restored queues, the 82-observation backlog, concurrent checks, user follow-ups, failed-window cooldown/recovery, errors, actual internal timeout and external cancellation.

Old passive-only expectations were updated explicitly. Manual-operation and read-only status tests still create unprocessed queues directly and assert their original contracts. All model replies are offline doubles. No live GLM rerun or FastAPI issue correctness verdict is implied. Restart iCode to load the changed module; the stopped session's files were not modified by this fix.

Final local validation: Smart Test selected the complete Compass integration directory and related architecture guards: **228 integration tests passed; 75 architecture checks passed; exit 0**. The 10 new regression cases passed, and the native-request reproduction was observed failing before the source change. Ruff check and format check passed on all 9 changed Python files. Source verification still validates all 36 recorded core modules. No live-model rerun, installation, commit or push was performed.
