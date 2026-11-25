# Compass migration to iCode main — 2026-11-24

## Working copy

- New directory: `/Users/yisenxu/Downloads/iCode-compass-main-20261124`.
- Branch: `codex/compass-main-20261124`.
- Upstream: `origin/main`, `acefa0ea639bff5c7265f1590630ff1a8d40ce80` (iCode 0.37.0).
- Source working copy: `/Users/yisenxu/Downloads/iCode-brief`, HEAD `432aa14f`.
- Original migration baseline: `90a3fb99c4d4ab476bdaf595bdace183e54dd7df`.

The source working copy contains uncommitted Compass work. Migration used its complete binary diff against the original baseline plus 89 untracked files under the Compass source, integration tests and documentation directories. It did not reset or overwrite the original working copy. A SHA-256 inventory confirmed that its 143 migration paths were unchanged after copying.

The only three-way conflict was in `scripts/chrys_test.py`. Both the new upstream workspace fixture selection rules and the Compass selection rule were retained. New upstream workspace and session behavior was preserved. No migration-specific change was needed inside Compass business code. New-main checks required these integration/test adaptations:

- The exact command-manual snapshot now includes the two existing Compass command entries. The byte-for-byte assertion remains in place.
- The exchange-walker guard has a narrowly scoped exception for `InformationInput.assistant_messages`: this scans one finalized model response, records call IDs only as audit metadata, and performs no exchange pairing. Using `iter_exchanges` here would drop call-free public replies. A behavior test preserves call-free replies and excludes tool/user text.
- The vocabulary guard exempts only the existing uv/Poetry/Gradle directory-option literals in the shell classifier, and the existing `working_dir_source` audit wire field in the adapter. Tests preserve uv/Poetry/Gradle parsing and the audit schema. The guard still applies elsewhere. A documentation phrase was updated to current repository terminology.


## Host changes relative to the new upstream

| File | Added lines |
| --- | ---: |
| `src/chrys/orchestration/engine/build/builder.py` | 9 |
| `src/chrys/app/tui/screens/main/commands.py` | 13 |
| `src/chrys/app/tui/screens/main/screen.py` | 13 |
| `src/chrys/foundation/platform/__init__.py` | 6 |
| Total | 41 |

This is not a zero-host-change integration. These counts exclude Compass source, packaging registration, tests, documentation and the Smart Test selection rule. Runner, bindings and serializers have no Compass diff against the new upstream. The migration does not replace native TUI callbacks with prompt shortcuts.

## Validation

- Independent virtual environment: `uv sync --python 3.14.0 --locked` succeeded; the old environment was not modified.
- Compass suite initially passed all 245 tests. Final run: **249 Compass tests + 4 command-localization tests = 253 passed** (9.13 seconds), including four new preservation cases.
- Command localization suite passed after its exact help snapshot was updated.
- Ruff and format checks passed for the migrated source/tests and final integration adaptations.
- `ty check --error-on-warning --python-platform ... src/chrys`: passed for darwin, linux and win32.
- Wheel build succeeded. The repository's `scripts/fetch_rg.sh` prepared the current-platform ripgrep 15.2.0 binary.
- A Python 3.14.0 isolated installation passed all 13 command-help checks, TUI/optional imports, bundled documentation, Simplified Chinese catalog, bundled ripgrep and Compass imports. It used a temporary tool environment, not the user's global install.
- Full architecture check after compatibility adaptations: **1,346 passed / 1 failed**, with the remaining failure caused only by a third-party flag quoted in this migration report. The wording was corrected; the complete vocabulary group then passed **4/4**. Thus every architecture assertion has passed on the final relevant contents, but this is a full run plus a targeted follow-up, not a claimed single all-green run. An earlier concurrent attempt aborted on its 60-second hygiene timeout; subsequent serial runs completed without that timeout.
- `git diff --check origin/main`: passed; no unresolved merge conflicts.

### Full Smart Test and remaining limitations

Smart Test auto-escalated to the complete local non-integration suite because `pyproject.toml` changed. The first complete run reported **41,114 passed, 7 failed, 137 skipped, 1 error**, plus three warnings. It therefore did not proceed to architecture checks; those were run separately.

| Initial failure/error | Follow-up result |
| --- | --- |
| Manual-page exact snapshot | Fixed by including the two intended Compass commands; localization suite passes. |
| Four `test_search_ignore_probe.py` cases | Reproduced identically in a clean `acefa0ea` main worktree using the same Python environment: 4 failed / 16 passed. Not introduced by the migration. |
| Workflow worker death with forked child retaining pipes | Reproduced in both migrated checkout and clean main: timeout in `test_worker_death_is_noticed_while_a_forked_child_holds_its_pipes`. Not introduced by the migration. |
| Workflow question modal wait | Passed in targeted serial recheck; original full-run timeout remains recorded. |
| Distribution catalog fixture | `uv build` could not find pinned Python 3.14.10. The explicitly selected 3.14.0 wheel build and installation probe succeeded. |

The unchanged official wheel script's archive check passed; its install stage also stopped because Python **3.14.10** is not installed locally. The repository pin is unchanged. The temporary 3.14.0-only probe does not establish compatibility under the pinned interpreter. The full 41k suite was not rerun after test-only adaptations; targeted follow-up results do not erase the initial failures or make this a globally green baseline.

These checks validate migration and packaging, not benchmark improvement or a completed live model run.

## Start the migrated copy

```sh
cd /Users/yisenxu/Downloads/iCode-compass-main-20261124
uv run --python 3.14.0 --no-sync icode
```

This runs the new checkout's editable installation. It does not replace an existing globally installed iCode. The existing host settings remain available; no credentials or session data were copied into the repository. No commit or push was performed.

## Local audit artifacts

`/private/tmp/compass-main-migration-20261124/` contains `tracked.patch`, `snapshot.json`, Compass/Smart Test logs, lint/type logs, wheel build/install logs and the Python 3.14.0-only installation probe. These are local migration artifacts, not release artifacts. Earlier review documents retain their original dated conclusions.
