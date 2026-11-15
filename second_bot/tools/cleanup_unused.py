#!/usr/bin/env python3
"""Audit unused-looking second_bot files; destructive cleanup is cache-only.

A JSON report is written before any deletion. Python sources, models, samples,
label.csv, tests, configuration/secrets, runtime data, and backups are never
removed by this tool.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

SAFE_SUFFIXES = {".pyc", ".pyo"}


def _text_references(root: Path, candidate: Path) -> list[str]:
    references: list[str] = []
    tokens = {candidate.name, candidate.stem, candidate.relative_to(root).as_posix()}
    for path in root.rglob("*"):
        if not path.is_file() or path == candidate or ".git" in path.parts:
            continue
        if path.suffix.lower() not in {".py", ".md", ".toml", ".ini", ".cfg", ".json", ".yml", ".yaml", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if any(token and token in text for token in tokens):
            references.append(path.relative_to(root).as_posix())
    return sorted(set(references))


def audit(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and path.name == "__pycache__":
            references = _text_references(root, path / "__init__.py") if (path / "__init__.py").exists() else []
            entries.append({"file": relative, "reason": "generated Python bytecode cache", "references": references, "alternative": "Python regenerates bytecode as needed", "safe_to_remove": True})
        elif path.is_file() and path.suffix.lower() in SAFE_SUFFIXES:
            entries.append({"file": relative, "reason": "generated Python bytecode", "references": _text_references(root, path), "alternative": "source .py file", "safe_to_remove": True})
        elif path.is_file() and path.suffix.lower() == ".py" and path.name in {"type1_local_ocr.py", "train_type1_local_ocr.py", "evaluate_type1_rgb.py"}:
            entries.append({"file": relative, "reason": "legacy compatibility facade retained after package extraction", "references": _text_references(root, path), "alternative": "second_bot/type1_ocr/ and second_bot/tools/; facade is retained for existing imports", "safe_to_remove": False})
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root_default = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--report", type=Path, default=root_default / "cleanup_unused_report.json")
    parser.add_argument("--apply", action="store_true", help="remove only reported .pyc/.pyo and __pycache__ after saving the report")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    findings = audit(root)
    report = {"root": str(root), "deletion_enabled": args.apply, "policy": "source, samples, models, tests, secrets, runtime data, and backups are never deleted", "findings": findings}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    removed = []
    if args.apply:
        for item in findings:
            if not item["safe_to_remove"]:
                continue
            path = root / str(item["file"])
            if path.is_dir() and path.name == "__pycache__":
                import shutil
                shutil.rmtree(path)
                removed.append(str(item["file"]))
            elif path.is_file() and path.suffix.lower() in SAFE_SUFFIXES:
                path.unlink()
                removed.append(str(item["file"]))
    print(json.dumps({"report": str(args.report), "removed": removed, "findings": findings}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
