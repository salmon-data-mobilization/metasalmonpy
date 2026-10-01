"""Prove emission changes preserve all old AST except types and warning category.

Run from this checkout: python .hub/evidence/B-58-unchanged-flow.py [base-ref].
The default is the exact published Python base used for B-58. This proof needs
Git objects, unlike the installed-package constructor/warning inventory guard.
"""
import ast
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
base = sys.argv[1] if len(sys.argv) > 1 else "e81cacd"
paths = subprocess.check_output(
    ["git", "diff", "--name-only", base, "--", "*.py"], cwd=root, text=True
).splitlines()

for name in paths:
    if "/" in name or name in {"__init__.py", "conditions.py"}:
        continue
    original = ast.parse(subprocess.check_output(
        ["git", "show", base + ":" + name], cwd=root, text=True
    ))
    current = ast.parse((root / name).read_text())
    aliases = {alias.asname: alias.name for node in current.body
               if isinstance(node, ast.ImportFrom) and node.module == "conditions"
               for alias in node.names}
    old_warns = [node for node in ast.walk(original)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "warnings.warn"]
    new_warns = [node for node in ast.walk(current)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "warnings.warn"]
    assert len(old_warns) == len(new_warns), name
    for old, new in zip(old_warns, new_warns):
        old_has_category = len(old.args) > 1 or any(key.arg == "category" for key in old.keywords)
        if not old_has_category:
            new.keywords = [key for key in new.keywords if key.arg != "category"]
    current.body = [node for node in current.body
                    if not (isinstance(node, ast.ImportFrom) and node.module == "conditions")]
    class Normalize(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id in aliases:
                node.id = node.id[1:]
            return node
    normalized = Normalize().visit(current)
    assert ast.dump(original, include_attributes=False) == ast.dump(normalized, include_attributes=False), name
    print(name + ": original control flow, messages, catches and chaining unchanged")
assert {"package_io.py", "llm_review.py"}.issubset(paths), "positive controls were not reached"
