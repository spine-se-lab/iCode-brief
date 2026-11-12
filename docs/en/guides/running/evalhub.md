# Run Compass v1.15 + iCode on EvalHub

Upload `integrations/evalhub/r5_icode_evalhub.py` and select `R5ICodeAgent`.
The upload contains startup and log collection code. Each task clones the specified branch into its sandbox, installs the locked dependencies, and runs iCode there.

## Job settings

Select the platform's DeepSeek Flash model. Its model identifier is forwarded to the API, including custom platform aliases. The sandbox must receive `OPENAI_API_KEY` and `OPENAI_BASE_URL` (or `OPENAI_API_BASE`) as string environment variables. Keep prompt templating disabled to use the original task text.

Set the agent arguments to the following JSON.

```json
{"repo_ref": "codex/compass-v1.15-evalhub"}
```

This branch is also the default. Every new task resolves its current HEAD and records the commit in `source-identity.json`. Optionally set `repo_commit` to a full 40-character commit SHA to reject an unexpected branch HEAD before any model calls. `workdir` defaults to the sandbox's current directory and accepts an explicit absolute task path.

## Startup and execution

1. Create a trial directory under `/logs/agent` and clone this repository with `git clone --depth 1 --single-branch --branch ...`.
2. Create a trial-local Python 3.14.0 environment and run `uv sync --frozen --no-dev --no-default-groups` with the repository lockfile.
3. Verify all 72 frozen Compass source files, then run iCode exploration, the Compass handoff, and Brief delivery.
4. Before submission, list all current requirements (excluding dropped entries) and request a check against the final diff. Execute repository commands in the assigned task workspace. Export the complete committed and uncommitted candidate for the platform verifier.

The sandbox needs Git and Python 3. If uv is absent, bootstrap installs it through the configured pip index. Python and dependency downloads follow uv's download and index configuration. Source checkout requires GitHub access. Dependencies are installed in the trial directory. A pre-provisioned Python 3.14 environment with the pinned dependencies can be selected through `runtime_python`.

## Version and budgets

- iCode 0.34.0 at `ffdc67ed74c1c2d78d16ab4b8544fed8d9ee3c69`.
- Frozen Compass v1.15, all 72 source files preserved byte for byte.
- Original archive SHA256 `3bc92bf4243046adddc3723b84d2dbc39eb928e6b6df60e36ab0b171b6d2b628`.
- Brief budget 15,000 characters with v1.15's initial full contract and subsequent requirement rotation.
- Main model output limit 64,000 tokens, `reasoning_effort=max`, `thinking.type=enabled`.
- Main-call budget 250, generation deadline 11,400 seconds.
- Maintenance uses the same job model and gateway with `temperature=0` and `thinking.type=disabled`.

Main-model temperature, top_p and top_k use provider defaults. Gateway acceptance and enforcement of reasoning parameters require platform verification. This branch starts at the evaluated iCode baseline. The newer iCode version on main needs a separate integration check before adoption.

## Artifacts and verification

`source-identity.json` records source identity. `runtime-setup.log` covers clone, installation and verification. `generation.log` and `agent/failure.json` diagnose execution failures. `agent/adapter-events.jsonl` records response and Brief events. `submission.json`, `submission.patch` and `outcome.json` record the exported candidate and completion state.

```bash
uv sync --frozen --python 3.14.0
uv run --no-sync --python 3.14.0 python integrations/evalhub/build_agent.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/test_agent.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/test_submission.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/smoke_native.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/smoke_sandbox.py
```

Smoke checks use synthetic tasks and mocked model HTTP responses through the real iCode CLI, handoff, tool and submission paths. The internal EvalHub deployment needs its own end-to-end execution check.
