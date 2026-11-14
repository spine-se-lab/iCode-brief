"""Build the standalone EvalHub upload from the adapter and sandbox bootstrap."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build():
    source = (ROOT / "agent_template.py").read_text(encoding="utf-8")
    source += "\nSANDBOX_BOOTSTRAP = " + repr((ROOT / "sandbox_agent.py").read_text(encoding="utf-8")) + "\n"
    compile(source, "r5_mini_evalhub.py", "exec")
    if len(source.encode("utf-8")) >= 1_000_000:
        raise ValueError("EvalHub upload exceeds 1 MB")
    (ROOT / "r5_mini_evalhub.py").write_text(source, encoding="utf-8")
    return source


if __name__ == "__main__":
    build()
