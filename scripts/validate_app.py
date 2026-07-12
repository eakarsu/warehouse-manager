#!/usr/bin/env python3
"""Validate a standalone merged application's manifest, export, and SQLite data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {
    "app_metadata", "source_projects", "features", "feature_evidence", "ai_pages", "ai_runs",
    "seed_table_registry", "seed_table_columns", "feature_table_links", "feature_navigation",
}


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    exported = json.loads((ROOT / "features.json").read_text())
    connection = sqlite3.connect(ROOT / "database.sqlite")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert REQUIRED_TABLES <= tables
        metadata_id = json.loads(connection.execute(
            "SELECT value FROM app_metadata WHERE key='id'"
        ).fetchone()[0])
        assert metadata_id == manifest["id"]
        database_features = connection.execute("SELECT id,canonical_key FROM features ORDER BY name,id").fetchall()
        assert len(database_features) == manifest["featureCount"] == len(exported)
        assert {row[1] for row in database_features} == {item["canonicalKey"] for item in exported}
        assert connection.execute("SELECT COUNT(*) FROM feature_navigation").fetchone()[0] == len(database_features)
        assert connection.execute("SELECT COUNT(*) FROM ai_pages").fetchone()[0] == manifest["aiPageCount"]
        bad_evidence = connection.execute("""SELECT COUNT(*) FROM features f
            WHERE f.evidence_count<>(SELECT COUNT(*) FROM feature_evidence e WHERE e.feature_id=f.id)""").fetchone()[0]
        assert bad_evidence == 0
        absolute_sources = connection.execute(
            "SELECT COUNT(*) FROM source_projects WHERE source_path LIKE '/%' OR source_path LIKE '%..%'"
        ).fetchone()[0]
        assert absolute_sources == 0
        assert connection.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == 0
    finally:
        connection.close()
    print(f"Validated {manifest['id']}: {len(exported)} features")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
