"""Safe SQLite backups, manifests and restore-dry-run verification."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path


def _db_files(root: str) -> list[Path]:
    base = Path(root)
    files = [base / "global_cosmo.db"]
    files.extend(sorted((base / "guilds_data").glob("guild_*.db")) if (base / "guilds_data").exists() else [])
    return [p for p in files if p.exists()]


def _integrity(path: Path) -> dict:
    result = {"path": path.name, "ok": False, "integrity": None, "tables": 0, "rows": {}}
    try:
        with sqlite3.connect(path, timeout=15) as conn:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity"] = str(check)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            result["tables"] = len(tables)
            for table in tables:
                safe = '"' + table.replace('"', '""') + '"'
                result["rows"][table] = int(conn.execute(f"SELECT COUNT(*) FROM {safe}").fetchone()[0])
            result["ok"] = check == "ok"
    except (sqlite3.DatabaseError, OSError) as exc:
        result["error"] = str(exc)[:300]
    return result


def create_backup(root: str, destination: str | None = None) -> dict:
    root = os.path.abspath(root)
    backup_root = Path(destination or (Path(root) / "backups"))
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_root / time.strftime("backup_%Y%m%d_%H%M%S")
    backup_dir.mkdir()
    manifest = {"created_at": int(time.time()), "files": [], "ok": True}
    for source in _db_files(root):
        relative = source.relative_to(root)
        target = backup_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source, timeout=15) as source_conn, sqlite3.connect(target) as target_conn:
            source_conn.backup(target_conn)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        check = _integrity(target)
        entry = {"file": str(relative), "sha256": digest, "bytes": target.stat().st_size, **check}
        manifest["files"].append(entry)
        manifest["ok"] = manifest["ok"] and check["ok"]
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": backup_dir.name, "path": str(backup_dir), **manifest}


def verify_backup(backup_path: str) -> dict:
    directory = Path(backup_path)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "error": "manifest.json غير موجود"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"manifest غير صالح: {exc}"}
    results = []
    ok = True
    for item in manifest.get("files", []):
        path = directory / item["file"]
        exists = path.exists()
        sha = hashlib.sha256(path.read_bytes()).hexdigest() if exists else None
        integrity = _integrity(path) if exists else {"ok": False, "error": "file missing"}
        current = {"file": item["file"], "exists": exists, "sha256_match": sha == item.get("sha256"), **integrity}
        results.append(current)
        ok = ok and current["exists"] and current["sha256_match"] and current["ok"]
    # Restore dry-run into an isolated directory and open every copied database.
    with tempfile.TemporaryDirectory(prefix="cosmo-restore-check-") as temp:
        for item in manifest.get("files", []):
            source = directory / item["file"]
            target = Path(temp) / item["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.exists(): shutil.copy2(source, target)
        for item in manifest.get("files", []):
            target = Path(temp) / item["file"]
            check = _integrity(target) if target.exists() else {"ok": False}
            ok = ok and check.get("ok", False)
    return {"ok": ok, "backup_id": directory.name, "files": results}


def list_backups(root: str) -> list[dict]:
    base = Path(root) / "backups"
    result = []
    for path in sorted(base.glob("backup_*/manifest.json"), reverse=True) if base.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": path.parent.name, "path": str(path.parent), "created_at": data.get("created_at"), "ok": data.get("ok", False), "files": len(data.get("files", []))})
        except (OSError, json.JSONDecodeError):
            result.append({"id": path.parent.name, "path": str(path.parent), "ok": False, "files": 0})
    return result
