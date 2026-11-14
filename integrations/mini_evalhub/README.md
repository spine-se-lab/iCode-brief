# Original R5 + mini-swe-agent for EvalHub

This adapter runs the original four-batch R5 workflow through mini-swe-agent 2.4.6. The 72 Compass runtime files under `../compass` match the frozen R5 manifest byte for byte. The source archive SHA256 is `62c304ae6315ffb1c977def04b77fb9c552ac75ceb533fd249c2b0180818d257`.

## Upload and configure

1. Upload `r5_mini_evalhub.py` in **Agents → Custom agents → Add an agent**. The file defines `R5MiniAgent`.
2. Set Arguments to the contents of the delivered `evalhub-arguments.json`. Its `repo_ref` selects `codex/r5-mini-original-evalhub`. Its `repo_commit` checks the exact downloaded commit. A moved branch fails setup so the new code can be reviewed before another run.
3. Select the platform's DeepSeek Flash model. The platform model alias is passed to the gateway unchanged. The gateway must inject `OPENAI_API_KEY` and `OPENAI_BASE_URL` into the sandbox. The base URL should be the OpenAI-compatible API prefix, such as `https://gateway.example/v1`.
4. Set Prompt template to `{{instruction}}`, or leave the template disabled. Leave custom agent Environment variables and model Extra parameters as `{}` for the existing platform-managed gateway configuration.
5. Select the existing DeepSWE dataset and its task environment. Enable OpenSandbox in the job using the platform's existing connection settings. Generation needs up to 11,400 seconds. Allow at least 12,000 seconds for the agent run, plus the separate 1,200-second setup allowance. If the UI offers one combined deadline, use at least 13,200 seconds. Keep the dataset's verifier timeout and test configuration.
6. Save the job and run the selected tasks. Harbor collects the candidate and runs the dataset verifier.

The default workdir is the sandbox's `pwd -P`. If the task environment's startup directory is different from its Git checkout, set the agent `workdir` argument to that checkout's absolute path.

## Runtime

The upload contains the Harbor adapter and a small sandbox bootstrap. During setup the sandbox clones the selected public branch, verifies `repo_commit`, installs the independent locked project in `integrations/mini_evalhub` with Python 3.14.0, and verifies all frozen R5 file hashes. A cached Python and dependency set can satisfy setup without new downloads.

The solver uses mini's native `mini.yaml`, tool-call parsing, history formatting and observation truncation. The original `ExploringCompassMiniAgent` performs exploration, `compass start`, G/R/K extraction, Brief delivery, evidence recording, Claim maintenance and the original submission check. `compass_review_before_submit` is false. External reference context is empty.

The transport maps the historical OpenRouter controls onto the platform's OpenAI-compatible gateway. The platform's model alias supplies both the main and maintenance model. Main requests carry `thinking={"type":"enabled"}`, `reasoning_effort="max"`, and `max_tokens=64000`. Main temperature and top-p are omitted, as in the frozen configuration. Top-k is omitted. Maintenance requests carry `thinking={"type":"disabled"}`, temperature 0, JSON-object response format, and the frozen 16,384-token cap with a bounded 32,768-token retry. The frozen core owns its two-attempt structured-call retry budget.

The main loop has a 250-step limit and a zero monetary stopping limit. Mini's native transport retries may produce several HTTP attempts for one step. The adapter records every observed response. A budget exit retains its status and candidate for verification. Each shell command has a 300-second maximum. Process ownership and a launch-once marker prevent duplicate launches when polling fails.

The sandbox retains the complete candidate, including commits, binary changes and new files. It exposes that candidate as a working-tree diff against the original task HEAD, verifies the exported tree, and writes `/logs/artifacts/patch.diff`. Harbor handles final grading.

## Network access

Setup uses `github.com`, `release-assets.githubusercontent.com` for downloadable CPython when needed, `pypi.org` and `files.pythonhosted.org` for locked dependencies. The platform model gateway must be reachable from the sandbox. A company package mirror can replace the Python package index if it serves the locked distributions. Repository test commands may require their own dataset-specific network allowlist.

## Evidence

The runner downloads these files from `/logs/agent/r5-<trial>/` when present.

- `source-identity.json` records the fetched branch and commit.
- `agent/adapter-events.jsonl` records requested reasoning settings, reported model names, numeric usage and reasoning-content presence. Credentials and request headers are excluded.
- `agent/trajectory.json` preserves the mini conversation and Compass metadata.
- `agent/run-status.json` reports exit status, main steps, contract size, Brief delivery and maintenance failures.
- `agent/compass-artifacts.tar.gz` contains the per-trial Compass records.
- `submission.json` and `submission.patch` identify the exact submitted candidate.

Wire logs prove that the adapter sent `reasoning_effort=max`. Confirmation that the provider honored it requires the gateway/provider's own request-routing evidence. The original runs used OpenRouter and host-isolated Docker. This adaptation uses EvalHub's gateway and task sandbox. Model routing, dependency closure, task environment, network policy and verifier can differ across those systems.

## Local checks

Run from the repository root with uv.

```sh
uv sync --project integrations/mini_evalhub --python 3.14.0
uv run --project integrations/mini_evalhub python integrations/mini_evalhub/build_agent.py
uv run --project integrations/mini_evalhub python -m unittest discover -s integrations/mini_evalhub -p 'test_*.py' -v
uv run --project integrations/mini_evalhub ruff check --no-fix integrations/mini_evalhub --exclude r5_mini_evalhub.py
uv run --project integrations/mini_evalhub ruff format --check integrations/mini_evalhub --exclude r5_mini_evalhub.py
```

The synthetic HTTP test exercises upload, clone, commit checking, setup, the frozen solver, Brief consumption, maintenance, committed patch export and application to a clean checkout. All model replies in these checks are synthetic. The internal EvalHub deployment still needs a platform run to establish its benchmark result.
