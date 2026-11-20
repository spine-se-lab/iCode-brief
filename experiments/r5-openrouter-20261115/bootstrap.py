"""Install the pinned official checkout and perform free checks in this case only."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
COMMIT = "78802f87d07f053fe560d680e4696c2bf322e2c2"
os.umask(0o077)
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ["PATH"]


def save(name, value):
    target = ROOT / name
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(target)


def main():
    checkout = ROOT / "iCode"
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/openJiuwen-ai/iCode.git", str(checkout)],
                   check=True, timeout=300)
    head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if head != COMMIT:
        subprocess.run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", COMMIT], check=True, timeout=300)
        subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", COMMIT], check=True)
    uv = shutil.which("uv")
    if uv is None:
        uv = next(str(p) for p in (Path.home() / ".local/bin/uv", Path.home() / "miniconda3/bin/uv") if p.is_file())
    subprocess.run([uv, "sync", "--frozen", "--no-dev", "--python", "3.14.7", "--project", str(checkout)],
                   check=True, timeout=900)
    python = str(checkout / ".venv/bin/python")
    env = dict(os.environ, PYTHONPATH=str(ROOT) + ":" + str(ROOT / "r5-frozen/src"))
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    for script in ("test_r5_native.py", "test_wire_audit.py", "test_run_batch.py", "prepare_tasks.py", "smoke_container_r5_native.py"):
        save("bootstrap-progress.json", {"at": time.time(), "script": script, "model_calls": 0})
        with (ROOT / (script + ".log")).open("x") as log:
            result = subprocess.run([python, str(ROOT / script)], cwd=ROOT, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=7200)
        save(script + ".result.json", {"at": time.time(), "returncode": result.returncode, "model_calls": 0})
        if result.returncode:
            raise RuntimeError("Free check failed: " + script)
    save("bootstrap-finished.json", {"at": time.time(), "passed": True, "model_calls": 0, "upstream": COMMIT})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        save("bootstrap-error.json", {"at": time.time(), "error_type": type(exc).__name__, "message": str(exc)})
        raise
