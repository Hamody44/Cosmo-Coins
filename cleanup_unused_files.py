#!/usr/bin/env python3
"""Safely report and optionally remove disposable project artifacts.

The normal no-argument mode cleans pip packages from base Python automatically after
the project's virtual environments are present. Explicit report modes remain dry runs.
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
import subprocess
import sys
import sysconfig
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
    ".local",
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
    "*requirements*.lock",
}
# These directories contain runtime code or assets loaded dynamically.
RUNTIME_DIRS = {"cogs", "dashboard", "second_bot"}
ENTRYPOINTS = {"main.py", "run_bots.py", "run_bots.sh"}
PROTECTED_USER_PACKAGES = {"pip", "setuptools", "wheel"}


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


def user_site_packages() -> Path:
    """Return the per-user site-packages directory, never a system directory."""
    configured = Path(sysconfig.get_paths().get("purelib", ""))
    user_base = Path.home() / ".local"
    candidates = [
        Path(site) for site in sys.path if site and user_base in Path(site).parents
    ]
    if candidates:
        return candidates[0]
    return user_base / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


def user_site_distributions() -> list[str]:
    """List package names installed in the user's site, excluding bootstrap tools."""
    site = user_site_packages()
    if not site.is_dir():
        return []
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--user", "--format=freeze"],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = []
    for line in result.stdout.splitlines():
        name = line.split("==", 1)[0].strip()
        if name and name.lower() not in PROTECTED_USER_PACKAGES:
            packages.append(name)
    return sorted(packages, key=str.casefold)


def global_pip_distributions() -> list[str]:
    """List pip-managed packages visible to base Python, excluding bootstrap tools."""
    if sys.prefix != sys.base_prefix:
        raise RuntimeError("Global cleanup must be run with base Python, not from inside a virtual environment.")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=freeze"],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = []
    for line in result.stdout.splitlines():
        name = line.split("==", 1)[0].strip()
        if name and name.lower() not in PROTECTED_USER_PACKAGES:
            packages.append(name)
    return sorted(set(packages), key=str.casefold)


def virtual_environments_exist(root: Path) -> bool:
    return all(
        (root / relative).is_file()
        for relative in (".venv/bin/python", "second_bot/.venv/bin/python")
    )


def remove_disposable(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        # pip uninstall may remove a cached file after the candidate list was built.
        return


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
    parser.add_argument(
        "--clean-user-site",
        action="store_true",
        help="Report packages in the current user's site-packages; never touches system Python packages.",
    )
    parser.add_argument(
        "--clean-global-site",
        action="store_true",
        help="Report pip packages visible to base Python outside virtual environments.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    automatic_global_cleanup = not any(
        (args.apply, args.include_source, args.delete_source, args.clean_user_site, args.clean_global_site)
    )
    if automatic_global_cleanup:
        args.apply = True
        args.clean_global_site = True
        print("Automatic mode: cleaning base-Python pip packages because no options were provided.")
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Root directory does not exist: {root}")

    disposable = disposable_candidates(root)
    source = source_candidates(root) if args.include_source or args.delete_source else []
    user_packages = user_site_distributions() if args.clean_user_site else []
    global_packages = global_pip_distributions() if args.clean_global_site else []

    print(f"Root: {root}")
    print("Protected: databases, logs, .env files, .runtime, backups, virtual environments, and requirements.")
    print(f"Disposable generated artifacts: {len(disposable)}")
    for path in disposable:
        print(f"  [generated] {path.relative_to(root)}")
    if source:
        print(f"Unreachable-looking Python files (manual review required): {len(source)}")
        for path in source:
            print(f"  [source]    {path.relative_to(root)}")
    if args.clean_user_site:
        print(f"User site-packages candidates: {len(user_packages)}")
        print(f"  [protected path] {user_site_packages()}")
        for package in user_packages:
            print(f"  [user package] {package}")
        print("System package paths such as /usr/lib and /usr/local/lib are never modified.")
    if args.clean_global_site:
        print(f"Global pip packages visible to base Python: {len(global_packages)}")
        for package in global_packages:
            print(f"  [global pip package] {package}")
        print("Only packages reported by base Python's pip are candidates; apt/system files are not scanned directly.")

    if not args.apply:
        print("Dry run only; nothing was deleted. Use --apply after reviewing the list.")
        return 0

    if args.delete_source:
        print("Refusing source deletion: this script intentionally never deletes source files automatically.")
        print("Review the report and remove confirmed files manually with version control protection.")
        return 2

    if args.clean_user_site and user_packages:
        if not virtual_environments_exist(root):
            print("Refusing user-site cleanup: both project virtual environments must exist first.")
            return 2
        if not args.apply:
            print("Dry run only; user packages were not removed. Use --apply after reviewing the list.")
            return 0
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "--yes", *user_packages],
            check=True,
        )
        print(f"Removed {len(user_packages)} user-site packages.")

    if args.clean_global_site and global_packages:
        if not virtual_environments_exist(root):
            print("Refusing global cleanup: both project virtual environments must exist first.")
            return 2
        if not args.apply:
            print("Dry run only; global pip packages were not removed. Use --apply after reviewing the list.")
            return 0
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "--yes", *global_packages],
            check=True,
        )
        print(f"Removed {len(global_packages)} global pip packages.")

    for path in disposable:
        remove_disposable(path)
    print(f"Deleted {len(disposable)} disposable generated artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
