"""Every import in the app package resolves, including the ones inside functions (code review 2026-10-29: a lazy
`from .gcp.llm import ...` moved into app/routes/ pointed at app.routes.gcp, so every real Gemini answer failed while
the tests, which replace the answerer, passed)."""
import ast
import importlib.util
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_every_relative_import_points_at_a_real_module():
    missing = []
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        mod = _module_name(path)
        package = mod if path.name == "__init__.py" else mod.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = "." * node.level + (node.module or "")
                try:
                    target = importlib.util.resolve_name(base, package)
                except ImportError:
                    missing.append(f"{path.relative_to(APP.parent)}:{node.lineno} {base}")
                    continue
                if node.module is None:   # from . import x: x is a submodule or a name in the package
                    continue
                try:
                    found = importlib.util.find_spec(target) is not None
                except ModuleNotFoundError:   # a parent package that does not exist
                    found = False
                if not found:
                    missing.append(f"{path.relative_to(APP.parent)}:{node.lineno} {base} -> {target}")
    assert not missing, "imports that point nowhere:\n" + "\n".join(missing)
