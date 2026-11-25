# Pagination follow-up and Brief delivery review — 2026-11-23

Scope: one task, one session, two dependent turns. This is a labeled post-hoc audit, not a benchmark result. Engineering changes were explicitly requested separately. Target: this iCode-brief development checkout; no host or runner changes, installation, commit, push, or new hosted-model call.

## Observed delivery

Session `55a3f2bb-0fa3-4258-ac46-5032072e6a10`; local source: `~/.chrys/sessions/55a3f2bb0fa3/trajectory/events.jsonl` and `session.json`. Event sequence numbers equal line numbers in this file. Raw private logs remain in their session directory.

| Turn | Requirements calls | Brief calls | Returned revision / evidence | Next request evidence |
| --- | ---: | ---: | --- | --- |
| Diagnose without edits | 1 | 1 | rev 1 / E-008 | events 146–148: result carrier `5f2d4de1f833c31cb4aea9ab406a291b` added to context `ae62581273ee1a3fbc8e511fa1abfa7f`, used by next model exchange |
| Now fix and add tests | 0 | 1 | rev 3 / E-013 | events 290–292: result carrier `8a2646a10013540a8cf65102e64005e1` added to context `6fd784e3fba64f0c032a7eb0cb57cc2e`, used by next model exchange |

The corresponding `function_result` messages are at zero-based indexes 10 and 23. Both contain a Brief. The second still states the earlier temporary no-edit goal. This establishes request delivery, not whether the model read or followed every sentence. Viewing `/compass status` and manually running `/compass` are not Agent delivery receipts.

Final reads/test (events 325–342) occurred after the second Brief call (274–288), and before turn completion (353). No later Brief tool call occurred in that turn; manual maintenance later processed through E-017. The fix does not force an extra completion pass or perform implicit model calls during observation.

## Root causes and changes

1. `gate/controller.py`: Brief initialization checked only missing requirements. Captured follow-ups could remain unapplied when the Agent skipped `update_compass_requirements`. Brief now checks captured user input against the extracted input before maintenance. A policy revision refreshes old checkpoints once. Ordinary code reads alone do not force extraction on each Brief check.
2. `task_contract/extraction.py` and `session.py`: extraction now receives chronological user messages and explicit supersession rules. Full dialogue remains available to the model, but deterministic fallback reconciliation uses only the newest message, avoiding resurrection of an earlier temporary no-edit sentence. Unrelated constraints must remain. Failed revision extraction keeps the prior contract and returns an error; it does not cache success or return a stale Brief as a successful refresh.
3. `claims/store.py`: replacing the contract invalidates its R/K coverage and R/K conflict notes. Reused R1 must not inherit an old task's conflict. Claim-to-claim tensions and immutable archived Briefs remain.
4. Status preview: securely read this session's saved `brief-*.txt` archives and navigate them in numeric revision order. Open current preview by default; Previous/Left goes older, Next/Right goes newer. Saved revisions and current preview are explicitly distinguished. Neither proves delivery. Empty/unreadable archives retain the current preview; unsafe files are rejected.

## Validation and limits

Only the newly added tests were run, per requested scope:

```sh
uv run --no-sync pytest tests/integration/compass/test_followup_refresh.py tests/integration/compass/test_brief_history.py -n0 -q
```

Result: **11 passed**. Includes the actual two Chinese task messages, native host request capture proving refreshed tool output reaches the next Agent request, restart/deduplication, extraction failure/retry, conflict invalidation, immutable archive preservation, numeric history navigation, empty/error history, and symlink refusal. Maintenance/model replies are controlled offline doubles. This does not establish GLM's semantic reliability or improved benchmark performance. Ruff checks and formatting cover changed Python files. The existing environment emits a patch-version mismatch warning under `--no-sync`; no environment was replaced.

To use: restart the iCode process launched from this development checkout; reopen the same session and run `/compass` once to refresh its old contract, then `/compass status` → Preview Brief. Historical Briefs retain their original text; only current state is reconciled. No need to reset the already repaired pagination demo merely to check Compass history or contract refresh.
