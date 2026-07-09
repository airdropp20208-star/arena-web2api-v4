#!/usr/bin/env python3
"""
Run all pending migrations on conversation store.

Usage:
    python3 migrate.py

Migrations live in migrations/*.py, each defines:
    VERSION = "x.y.z"
    def migrate(data: list[dict]) -> list[dict]: ...

Backups original file to .pre-migration.bak before writing.
"""
import json
import os
import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, ".")
from src.config import CONVERSATION_STORE_FILE


def main() -> int:
    if not CONVERSATION_STORE_FILE:
        print("CONVERSATION_STORE_FILE not set — nothing to migrate")
        return 0

    if not os.path.exists(CONVERSATION_STORE_FILE):
        print(f"Store file not found: {CONVERSATION_STORE_FILE} — fresh start, no migration needed")
        return 0

    # Load current data
    try:
        with open(CONVERSATION_STORE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Store file corrupt: {e}")
        print("  Try loading from .bak files manually:")
        for path in [
            CONVERSATION_STORE_FILE + ".bak",
            CONVERSATION_STORE_FILE + ".bak.1",
            CONVERSATION_STORE_FILE + ".bak.2",
        ]:
            if os.path.exists(path):
                print(f"    cp {path} {CONVERSATION_STORE_FILE}")
        return 1

    print(f"Loaded {len(data)} conversations from {CONVERSATION_STORE_FILE}")

    # Run migrations
    migrations_dir = Path("migrations")
    if not migrations_dir.exists():
        print("No migrations dir — nothing to do")
        return 0

    migration_files = sorted(migrations_dir.glob("*.py"))
    migration_files = [f for f in migration_files if f.name != "__init__.py"]

    if not migration_files:
        print("No migration files — nothing to do")
        return 0

    print(f"\nRunning {len(migration_files)} migrations:")
    for mf in migration_files:
        try:
            spec = importlib.util.spec_from_file_location(mf.stem, mf)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            version = getattr(mod, "VERSION", "unknown")
            if hasattr(mod, "migrate"):
                before = len(data)
                data = mod.migrate(data)
                after = len(data)
                print(f"  ✓ {mf.name} (v{version}): {before} → {after} conversations")
            else:
                print(f"  ⚠ {mf.name}: no migrate() function, skip")
        except Exception as e:
            print(f"  ❌ {mf.name}: {e}")
            return 1

    # Backup original
    backup_path = CONVERSATION_STORE_FILE + ".pre-migration.bak"
    try:
        import shutil
        shutil.copy2(CONVERSATION_STORE_FILE, backup_path)
        print(f"\n✓ Backup: {backup_path}")
    except Exception as e:
        print(f"⚠ Backup failed: {e}")

    # Write migrated
    try:
        with open(CONVERSATION_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"✓ Migrated data written to {CONVERSATION_STORE_FILE}")
    except Exception as e:
        print(f"❌ Write failed: {e}")
        print(f"  Restore from backup: cp {backup_path} {CONVERSATION_STORE_FILE}")
        return 1

    print("\n✓ All migrations applied successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
