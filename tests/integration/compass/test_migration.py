"""Migration guards: preserve core semantics and the host-independent boundary."""

import ast
import hashlib
import json

from compass.provenance import SOURCE_ROOT, sha, source_constants


def normalized_tree(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "compass.ports":
                node.module = "compass_brief.ports"
            elif node.module.startswith("compass.gate."):
                node.module = node.module.replace("compass.gate.", "compass_brief.intervention.", 1)
            elif node.module.startswith("compass."):
                node.module = node.module.replace("compass.", "compass_brief.", 1)
    # The sole I/O adjustment: git status never reads stdin. Pin the exact
    # DEVNULL expression, then compare every other part with the original AST.
    for function in tree.body:
        if isinstance(function, ast.AsyncFunctionDef) and function.name == "_git_dirty_paths":
            for call in ast.walk(function):
                if isinstance(call, ast.Call) and ast.unparse(call.func) == "asyncio.create_subprocess_exec":
                    stdin = [keyword for keyword in call.keywords if keyword.arg == "stdin"]
                    assert len(stdin) == 1
                    assert ast.unparse(stdin[0].value) == "asyncio.subprocess.DEVNULL"
                    call.keywords.remove(stdin[0])
    # isort only reorders adjacent imports. Ignore that and source formatting.
    result, imports = [], []
    for node in [*tree.body, None]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        else:
            result.extend(sorted(imports, key=ast.dump))
            imports = []
            if node is not None:
                result.append(node)
    tree.body = result
    return ast.dump(tree, include_attributes=False)


def test_original_core_semantics_and_prompts_preserved():
    provenance = json.loads((SOURCE_ROOT / "provenance.json").read_text())
    assert len(provenance["original"]["files_sha256"]) == 72
    assert len(provenance["files"]) == 36
    for path, record in provenance["files"].items():
        source = (SOURCE_ROOT / path).read_text()
        assert hashlib.sha256(source.encode()).hexdigest() == record["sha256"], path
        assert sha(normalized_tree(source)) == record.get("adapted_ast_sha256", record["original_ast_sha256"]), path
    prompts = source_constants(SOURCE_ROOT)
    assert {name: sha(value) for name, value in prompts.items()} == provenance["submission_sha256"]


def test_core_and_runtime_do_not_import_host_or_model_sdks():
    paths = [
        p for p in SOURCE_ROOT.rglob("*.py") if "adapters" not in p.parts and p.name not in {"cli.py", "__main__.py"}
    ]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert all(not name.startswith(("chrys", "openai", "compass.adapters")) for name in names), path
