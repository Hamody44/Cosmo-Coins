#!/usr/bin/env python3
"""Safely report and optionally remove disposable project artifacts.

The default mode is a dry run. It only reports candidates and never deletes files.
With --apply, only clearly disposable generated artifacts are removed. Source files,
backups, databases, environment files, virtual environments, and runtime state are
always protected unless the caller explicitly opts into the dangerous source mode.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import shutil
from pathlib import Path


DISPOSABLE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".hypothesis",
}
DISPOSABLE_FILE_PATTERNS = {
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".coverage",
    ".coverage.*",
}
PROTECTED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".runtime",
    "guilds_data",
    "backups",
    "node_modules",
}
PROTECTED_FILE_PATTERNS = {
    ".env",
    ".env.*",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.tar.gz",
    "requirements*.txt",
    "requirements*.in",
}
# These directories contain runtime code or assets loaded dynamically.
RUNTIME_DIRS = {"cogs", "dashboard", "second_bot"}
ENTRYPOINTS = {"main.py", "run_bots.py", "run_bots.sh"}


def is_in_dir(path: Path, names: set[str], root: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in names for part in relative_parts)


def is_protected(path: Path, root: Path) -> bool:
    if is_in_dir(path, PROTECTED_DIRS, root):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in PROTECTED_FILE_PATTERNS)


def is_disposable(path: Path, root: Path) -> bool:
    if is_protected(path, root):
        return False
    if is_in_dir(path, DISPOSABLE_DIRS, root):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in DISPOSABLE_FILE_PATTERNS)


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or is_protected(path, root):
            continue
        yield path


def imported_project_modules(path: Path, root: Path) -> set[Path]:
    """Return local Python files directly referenced by one Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    result: set[Path] = set()
    for module in modules:
        parts = module.split(".")
        candidate = root.joinpath(*parts).with_suffix(".py")
        package_init = root.joinpath(*parts, "__init__.py")
        if candidate.is_file():
            result.add(candidate.resolve())
        elif package_init.is_file():
            result.add(package_init.resolve())
    return result


def reachable_source_files(root: Path) -> set[Path]:
    """Approximate source reachability while protecting dynamic runtime folders."""
    reachable: set[Path] = set()
    pending = [root / name for name in ENTRYPOINTS if (root / name).is_file()]
    pending.extend(
        path for folder in RUNTIME_DIRS if (root / folder).is_dir() for path in (root / folder).rglob("*.py")
    )

    while pending:
        current = pending.pop().resolve()
        if current in reachable or not current.is_file():
            continue
        reachable.add(current)
        pending.extend(imported_project_modules(current, root))
    return reachable


def source_candidates(root: Path) -> list[Path]:
    reachable = reachable_source_files(root)
    candidates = []
    for path in iter_files(root):
        if path.suffix != ".py" or path.resolve() in reachable:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.name.startswith("test_") or "tests" in path.relative_to(root).parts:
            continue
        candidates.append(path)
    return sorted(candidates)


def disposable_candidates(root: Path) -> list[Path]:
    return sorted(path for path in iter_files(root) if is_disposable(path, root))


def remove_disposable(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report unused-looking files safely; deletion is dry-run by default."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete disposable generated artifacts (never source/data files).",
    )
    parser.add_argument(
        "--include-source",
        action="store_true",
        help="Include unreachable Python source files in the report; does not delete them.",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Dangerous: with --apply, delete reported unreachable Python files too.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Root directory does not exist: {root}")

    disposable = disposable_candidates(root)
    source = source_candidates(root) if args.include_source or args.delete_source else []

    print(f"Root: {root}")
    print("Protected: databases, logs, .env files, .runtime, backups, virtual environments, and requirements.")
    print(f"Disposable generated artifacts: {len(disposable)}")
    for path in disposable:
        print(f"  [generated] {path.relative_to(root)}")
    if source:
        print(f"Unreachable-looking Python files (manual review required): {len(source)}")
        for path in source:
            print(f"  [source]    {path.relative_to(root)}")

    if not args.apply:
        print("Dry run only; nothing was deleted. Use --apply after reviewing the list.")
        return 0

    if args.delete_source:
        print("Refusing source deletion: this script intentionally never deletes source files automatically.")
        print("Review the report and remove confirmed files manually with version control protection.")
        return 2

    for path in disposable:
        remove_disposable(path)
    print(f"Deleted {len(disposable)} disposable generated artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
