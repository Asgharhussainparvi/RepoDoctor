"""Architecture rule: only repodoctor/sandbox/manager.py may execute processes."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "repodoctor"
ALLOWED = PACKAGE / "sandbox" / "manager.py"
FORBIDDEN_MODULES = {"subprocess", "pty", "docker", "multiprocessing", "pexpect"}
FORBIDDEN_OS_ATTRS = {
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execl",
    "execlp",
    "spawnv",
    "spawnl",
    "startfile",
}


def violations(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [
                a.name for a in node.names if a.name.split(".")[0] in FORBIDDEN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(node.module)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                found.append(f"os.{node.attr}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"eval", "exec", "__import__"}:
                found.append(node.func.id)
    return found


def test_only_sandbox_manager_executes_processes():
    offenders = {
        str(p.relative_to(PACKAGE)): v
        for p in PACKAGE.rglob("*.py")
        if p != ALLOWED and (v := violations(p))
    }
    assert offenders == {}


def test_the_allowed_module_really_is_the_one_boundary():
    assert "subprocess" in violations(ALLOWED)
