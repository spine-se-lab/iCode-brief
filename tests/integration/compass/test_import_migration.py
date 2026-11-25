"""Check Compass imports even when they are inside unexecuted test functions."""

import ast
import importlib
from pathlib import Path

import pytest

from compass.provenance import SOURCE_ROOT


def validate_compass_imports(source):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or not node.module or not node.module.startswith("compass"):
            continue
        module = importlib.import_module(node.module)
        for item in node.names:
            if item.name == "*" or hasattr(module, item.name):
                continue
            # `from compass import cli` can name a submodule rather than an export.
            importlib.import_module(node.module + "." + item.name)


def test_all_compass_source_and_test_imports_resolve():
    tests = Path(__file__).parent
    for path in [*SOURCE_ROOT.rglob("*.py"), *tests.rglob("*.py")]:
        try:
            validate_compass_imports(path.read_text(encoding="utf-8"))
        except (ImportError, AttributeError) as exc:
            pytest.fail(f"Unresolved Compass import in {path}: {exc}")


@pytest.mark.parametrize(
    "source",
    [
        "def nested():\n    from compass.provenance import MAX_SECONDS\n",
        "def nested():\n    from compass.evaluation.session import Controller\n",
    ],
)
def test_import_guard_rejects_stale_nested_imports(source):
    with pytest.raises(ImportError):
        validate_compass_imports(source)
