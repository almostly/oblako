"""Every function and class in the package has a docstring, private and nested ones too.

pydocstyle (the pre-commit hook) checks the docstrings' style, but asks for one only
on public names: a ``_helper`` or a function inside another goes unchecked. This test
asks for one everywhere the hook looks (the package, less the dashboard).
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "oblako"


def test_every_function_and_class_has_a_docstring():
    """Fail with the location of each def or class that has no docstring."""
    missing = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "dashboard" in path.relative_to(PACKAGE).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if ast.get_docstring(node) is None:
                    missing.append(
                        f"{path.relative_to(PACKAGE.parent)}:{node.lineno} {node.name}"
                    )
    assert not missing, "no docstring:\n" + "\n".join(missing)
