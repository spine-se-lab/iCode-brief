# Frozen R5 + iCode 0.36.0 experiment

This branch preserves the **original November 15, 2026 OpenRouter experiment**:
113 DeepSWE tasks × 4 rounds (452 generation attempts). It is an archival
reproduction branch, not the current Compass extension implementation.

**Do not merge new main changes, refactor, or fix the archived runtime here.**
Create a different branch/tag for any change. Check out the immutable commit or
release tag `reproduce-r5-icode-0.36.0-20261115-v1` when reproducing.

## What is fixed

| Component | Identity / setting |
| --- | --- |
| iCode | `iCode-TUI==0.36.0`, upstream commit `78802f87d07f053fe560d680e4696c2bf322e2c2` |
| Actual experiment Python | **3.14.7** (explicit override; upstream `.python-version` says 3.14.10) |
| Frozen R5 | 72 manifest-listed files, byte-for-byte copies of the experiment |
| Original R5 package SHA-256 | `62c304ae6315ffb1c977def04b77fb9c552ac75ceb533fd249c2b0180818d257` |
| Native adapter SHA-256 | `e8ba9faed2b9ddecba7b03985356d5e8fe987e2aa1dc511d83a5085373e562f5` |
| Endpoint | `https://openrouter.ai/api/v1` |
| Model | `deepseek/deepseek-v4.1-flash-1017` |
| Provider routing | DeepSeek only; no fallback; `require_parameters=true` |
| Main model | `reasoning.effort=max`, `max_tokens=64000`; non-streaming |
| Sampling | temperature / top_p / top_k omitted for main calls |
| Maintenance model | same model; reasoning disabled; temperature 0; JSON output |
| Per-task main budget | 250 calls / 11,400 seconds |
| Original retry behavior | SDK retries 2; iCode transient retries **0** |
| Each of two hosts | 4 task pipelines, 2 concurrent verifications; same base serialized |
| Generation | Docker Desktop `desktop-linux`, linux/amd64, network none, zero mounts, 2 CPU / 5 GiB |
| Verification | bridge network, build network none, 2 CPU / 5 GiB, 3,600 seconds |

Upstream files at the repository root are unchanged from that exact iCode
commit. All additions are in this document and `experiments/r5-openrouter-20261115/`.
`FROZEN-MANIFEST.json` hashes the 90 archived source/metadata files.
`reproduce.py` is new **local staging/launch plumbing**, replacing private SSH
orchestration. It does not change the original adapter, R5, scheduler, or graders.

The R5 source manifest records an original uncommitted runtime delta. Use the
bundled files, not just the manifest's source Git commit. The package digest is
historical provenance, not the digest of a newly generated tarball.

## Reproduce on two dedicated machines

Prerequisites: Git, uv, macOS with Docker Desktop (`desktop-linux` context),
linux/amd64 emulation, at least 60 GiB free at launch, no other running
containers. The two machines need not have the original addresses: `95` and
`96` below are only assignment labels (rounds 1+3 and rounds 2+4).

Obtain the official DeepSWE tasks and image access separately. Place them at
`$RUN_BASE/deep-swe/tasks/<task>/` including `instruction.md`, `task.toml`, and
the official verifier assets. The 113 task names, exact test-file hashes and
image digest/layers are in `selection.json` and `task-identities.json`.
Preparation refuses mismatching assets. This repository contains **no task
solutions, gold patches, hidden test contents, API keys, or historical run logs**.
Verifier assets stay on the host; only public task instructions are sent to the
solver. Do not put this reproduction repository or verifier assets inside the
solver container.

### 1. Get the frozen code and prepare one host

```bash
git clone --branch reproduce-r5-icode-0.36.0-20261115-v1 \
  https://github.com/spine-se-lab/iCode-brief.git icode-r5-frozen
cd icode-r5-frozen
ARCHIVE="$PWD/experiments/r5-openrouter-20261115"
RUN_BASE="$HOME/r5-reproduction"  # dedicated new directory on each host
python3 "$ARCHIVE/reproduce.py" verify
python3 "$ARCHIVE/reproduce.py" stage --base "$RUN_BASE" --host 95
# Use --host 96 on the second machine. Do not stage twice into the same base.
RUN_ROOT="$RUN_BASE/icode-r5-openrouter-20261115"
```

Place the separately obtained official dataset under the base described above.
Then run the historical bootstrap:

```bash
python3 "$RUN_ROOT/bootstrap.py"
```

Bootstrap fetches the exact upstream Git commit, installs the `uv.lock`
dependencies using Python 3.14.7, runs the 12 offline adapter tests, prepares all
113 task images, and runs the scripted-HTTP container smoke test. It downloads
code/dependencies/images, but makes **zero paid model calls**. The nested
`$RUN_ROOT/iCode` checkout is intentional: the historical adapter verifies that
its HEAD equals the original upstream commit, not the archival branch's new
commit. Preparation failures require inspection; do not blindly repeat it.

### 2. Configure the credential and confirm routing

Store your own OpenRouter API key in
`$RUN_ROOT/.secrets/openrouter.key`, as an owner-only regular file (mode 0600).
Do not commit it or pass it through the solver container environment. The
original container-mode runner keeps the credential on the host. Do not run
the historical adapter directly in unisolated host mode on untrusted tasks.

The original experiment separately verified an OpenRouter upstream request
with max reasoning. Before claiming the same routing in a new experiment,
independently confirm the current provider is DeepSeek and the upstream
reasoning parameter is max. Keep a **sanitized confirmation JSON**, with
`"passed": true` plus the method, time, model/provider and parameter evidence;
never include a key or authorization headers. The launcher requires this file,
but does not revalidate its contents against OpenRouter or perform a paid probe.
A client-side `wire_request` proves what was sent to OpenRouter; it alone does
not prove what the upstream provider received. Model availability/provider
behavior can change even though source code is frozen.

### 3. Explicitly start paid generation

After all free checks succeed (preflight must be under one hour old):

```bash
python3 "$ARCHIVE/reproduce.py" launch --base "$RUN_BASE" \
  --upstream-max-confirmation /absolute/path/to/sanitized-confirmation.json \
  --accept-paid-226
```

Run once on each host: **226 attempts per host, 452 in total**. This command
starts a background worker. It rejects existing launch markers and never
resumes or adds retries automatically. The original worker owns both global
locks while running. If a launch is uncertain or a worker stops, inspect the
saved evidence before deciding on a separately recorded continuation.

Read `$RUN_ROOT/status.json`, `finished.json`, `batch.log`, and
`runs/<slot>/result.json`. Do not expose unredacted model logs or credentials.
Verification results are under
`$RUN_BASE/deepswe-runs/icode-r5-openrouter-eval-20261115/jobs/`.

## Interpretation and boundaries

- This is the original four-round code, including transient retry limit 0.
  Later Clack/Koota regeneration fixes and supplementary attempts are excluded.
- Mac mini re-verifications and different-patch reruns are separate experiments.
  Keep original results and patch hashes; do not silently overwrite them.
- The earlier 86-pass iCode 0.34.0 batch is **not** part of this 0.36.0 archive.
- `patch_status` and `status` are different: a valid patch score does not prove
  that the R5 treatment (Brief handoff/consumption) was valid. Missing verifier
  reports remain unscored. Do not turn missing evidence into pass/fail.
- Reproduction means fixed source, parameters, identities and procedure, not a
  guarantee of identical stochastic model outputs or pass counts.
- No evaluation was launched when preparing this archive. Offline test results
  and archive checks are recorded in `VALIDATION.md` next to the manifest.

中文说明：这是原始 R5 + iCode 0.36.0 四轮实验的冻结分支，不是后来迁移到
Extension 的开发版。原实验代码逐字节保留；新脚本只负责在别人的机器上准备目录
和启动任务。后续修复另建分支，本分支不继续更新。复现时请固定 tag 或 commit，
不要使用会移动的 main。实验数据集、镜像访问权限和自己的 API key 需要另行准备。
