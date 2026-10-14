"""Test helper: install a tiny extension the way the manager would, without git.

``install_fixture`` writes the files to ``<extensions_dir>/<name>/``, pins the tree digest
and saves an ``enabled`` row with the manifest snapshot. The host then loads it like any
installed extension. For tests and the e2e suite only; it never touches ``~/.resonant``
unless ``settings`` points there.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from resonant.config import Settings
from resonant.extensions.pin import tree_digest
from resonant.store.db import atomic, utcnow
from resonant.store.extensions import ExtensionRecord, save_extension
from resonant_sdk.manifest import Manifest, parse_yaml

FAKE_COMMIT = "a1b2c3d4" * 5


def install_fixture(
    settings: Settings,
    conn: sqlite3.Connection,
    manifest_yaml: str,
    files: Mapping[str, str],
    *,
    argv0_paths: Mapping[str, str] | None = None,
    executable: tuple[str, ...] = (),
) -> ExtensionRecord:
    """Write ``resonant.yaml`` plus ``files``, pin them and save the row. Returns it."""
    manifest = Manifest.model_validate(parse_yaml(manifest_yaml))
    root = settings.extensions_dir / manifest.name
    root.mkdir(parents=True, exist_ok=True)
    (root / "resonant.yaml").write_text(manifest_yaml)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if rel in executable:
            path.chmod(0o755)
    record = ExtensionRecord(
        name=manifest.name,
        source=f"repo:{manifest.name}",
        commit=FAKE_COMMIT,
        tree_digest=tree_digest(root),
        manifest_sha=hashlib.sha256(manifest_yaml.encode()).hexdigest(),
        manifest=manifest.model_dump(mode="json", exclude_defaults=True),
        installed_at=utcnow(),
        installed_by="owner:cli",
        argv0_paths=dict(argv0_paths or {}),
    )
    with atomic(conn):
        save_extension(conn, record)
    return record


def edit(root: Path, rel: str, text: str) -> None:
    """Change a file in an installed copy (a tamper, for tests)."""
    (root / rel).write_text(text)
