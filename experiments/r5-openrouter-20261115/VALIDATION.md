# Archive validation — 2026-11-20

- All 90 archived files match the local original experiment copies byte for byte.
- All 72 R5 files match the original `SOURCE-MANIFEST.json`.
- The original `mini/presentation.py` has an extra final blank line; it is
  intentionally retained to preserve its recorded hash (`git diff --check`
  reports this historical whitespace).
- Adapter and four grader hashes match the original experiment provenance.
- Upstream tracked files are unchanged from `78802f87d07f053fe560d680e4696c2bf322e2c2`.
- Archived-source checks: 7 adapter + 3 wire-audit + 2 scheduler tests passed.
- New local plumbing: 8 tests passed, covering both assignments, integrity,
  exclusive staging, repeated-launch rejection, missing max confirmation,
  stale preflight, and a fully mocked launch/receipt path.
- No paid model calls, benchmark generation, Docker trials or remote workers
  were started for archive validation.
- Tests used the existing local iCode 0.36.0 environment, **Python 3.14.0**,
  via `uv run --no-sync`; they did not install or alter that environment.
  The recorded historical experiment used **3.14.7**. The archived bootstrap
  explicitly installs 3.14.7 and the launcher checks it. A fresh installation
  and container smoke test on 3.14.7 were not rerun for this archival change.
- Explicit source/metadata allowlist; no historical run outputs, credentials,
  task instructions, gold patches or hidden test contents were copied.
  Secret-pattern checks found no API token/private key values in the additions.

The first local test attempt was blocked by access to uv's default cache; it
was rerun successfully with an isolated temporary uv cache. This was not a
runtime or test failure in the archived code.

## Repeat the checks without a model

After staging and bootstrap installation, from the staged directory:

```bash
export PYTHONPATH="$PWD:$PWD/r5-frozen/src"
uv run --no-sync --project iCode python test_r5_native.py
uv run --no-sync --project iCode python test_wire_audit.py
uv run --no-sync --project iCode python test_run_batch.py
```

The new helper checks need only Python's standard library. From the archival
repository root (not the staged runtime directory):

```bash
python3 experiments/r5-openrouter-20261115/reproduce.py verify
python3 experiments/r5-openrouter-20261115/test_reproduce.py
```
