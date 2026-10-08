"""Snapshots, so that losing the corpus costs minutes rather than a day.

The corpus this protects is not cheap. Rebuilding it means resolving ~66 company domains
to ATS boards, sweeping every board, researching each company, and scoring every posting —
hours of other people's servers and a chunk of several metered free tiers. It was lost once
to a test that dropped the wrong database, and the guard in `safety.py` stops that
particular accident. This exists for the others: a bad migration, a mistaken bulk delete,
an experiment that went further than intended.

Deliberately simple. SQLite is a file, so a snapshot is a copy made through SQLite's own
backup API — which is safe against a database being written to at the time, unlike `cp`.
Postgres is left to `pg_dump`, which is the right tool and not one to reimplement badly;
this reports what to run rather than pretending to do it.

A restore is destructive, so it goes through the same guard as everything else and asks to
be forced, and it snapshots what it is about to replace first. Restoring the wrong backup
is a mistake somebody makes exactly once, and then wants the previous state back.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging import get_logger
from .safety import assert_destructive_allowed, current_environment

log = get_logger("storage.backup")

# Snapshots live beside the database rather than in the project tree, so they are never
# committed and never shipped.
BACKUP_DIRNAME = "backups"

# Older snapshots are pruned. Twelve is a couple of weeks of daily use without the
# directory growing without bound.
KEEP = 12


@dataclass
class Snapshot:
    path: Path
    taken_at: datetime
    bytes: int
    label: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "taken_at": self.taken_at.isoformat(),
            "bytes": self.bytes,
            "megabytes": round(self.bytes / 1_000_000, 2),
            "label": self.label,
        }


class BackupError(RuntimeError):
    """A snapshot or restore could not be done, with a reason worth showing."""


def _sqlite_path(url: str) -> Path | None:
    """The file a SQLite URL points at, or None if it is not a file-backed SQLite URL."""
    lowered = (url or "").strip()
    if not lowered.lower().startswith("sqlite"):
        return None
    if "///" not in lowered:
        return None
    raw = lowered.split("///", 1)[1].split("?", 1)[0]
    if not raw or ":memory:" in raw:
        return None
    return Path(raw)


def backup_dir(url: str) -> Path:
    path = _sqlite_path(url)
    base = path.parent if path else Path("data")
    directory = base / BACKUP_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_snapshots(url: str) -> list[Snapshot]:
    """Every snapshot, newest first."""
    out: list[Snapshot] = []
    for file in backup_dir(url).glob("*.db"):
        try:
            stat = file.stat()
        except OSError:
            continue
        label = ""
        parts = file.stem.split("__", 1)
        if len(parts) == 2:
            label = parts[1].replace("_", " ")
        out.append(
            Snapshot(
                path=file,
                taken_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                bytes=stat.st_size,
                label=label,
            )
        )
    return sorted(out, key=lambda s: s.taken_at, reverse=True)


def _prune(url: str, keep: int = KEEP) -> int:
    removed = 0
    for snapshot in list_snapshots(url)[keep:]:
        try:
            snapshot.path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def snapshot(url: str, *, label: str = "") -> Snapshot:
    """Copy the database to a timestamped file.

    Uses SQLite's own backup API rather than a file copy: it is safe to run against a
    database that is being written to, where `shutil.copy` would capture a torn page.
    """
    path = _sqlite_path(url)
    if path is None:
        raise BackupError(
            "snapshots are implemented for file-backed SQLite. For Postgres use "
            "`pg_dump`, which is the right tool and not one to reimplement here."
        )
    if not path.exists():
        raise BackupError(f"no database at {path}")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(c if c.isalnum() else "_" for c in label).strip("_")
    target = backup_dir(url) / (f"{stamp}__{safe_label}.db" if safe_label else f"{stamp}.db")

    source = sqlite3.connect(str(path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    stat = target.stat()
    pruned = _prune(url)
    log.info("snapshot_taken", path=str(target), bytes=stat.st_size, pruned=pruned)
    return Snapshot(
        path=target,
        taken_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        bytes=stat.st_size,
        label=label,
    )


def restore(url: str, name: str, *, force: bool = False) -> Snapshot:
    """Replace the database with a snapshot. Destructive, and guarded as such.

    Takes a snapshot of the current state first. Restoring the wrong backup is a mistake
    somebody makes exactly once, and then wants the previous state back.
    """
    assert_destructive_allowed(url, f"restore over the database from {name!r}", force=force)

    path = _sqlite_path(url)
    if path is None:
        raise BackupError("restore is implemented for file-backed SQLite only")

    candidates = [s for s in list_snapshots(url) if s.name == name or s.path.stem == name]
    if not candidates:
        available = ", ".join(s.name for s in list_snapshots(url)[:5]) or "none"
        raise BackupError(f"no snapshot named {name!r}. Available: {available}")
    chosen = candidates[0]

    if path.exists():
        snapshot(url, label="before restore")

    shutil.copy2(chosen.path, path)
    log.warning("database_restored", source=chosen.name, target=str(path))
    return chosen


def health(url: str) -> dict[str, Any]:
    """Whether this database is intact, and what protection it has."""
    path = _sqlite_path(url)
    snapshots = list_snapshots(url)
    latest = snapshots[0] if snapshots else None

    integrity = "unknown"
    tables = 0
    if path and path.exists():
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                tables = int(
                    connection.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            integrity = f"unreadable: {type(exc).__name__}"

    return {
        "environment": current_environment().value,
        "scheme": (url or "").split("://", 1)[0],
        "exists": bool(path and path.exists()),
        "bytes": path.stat().st_size if path and path.exists() else 0,
        "tables": tables,
        "integrity": integrity,
        "snapshots": len(snapshots),
        "latest_snapshot": latest.to_dict() if latest else None,
        "note": (
            "No snapshot yet. Take one before any migration or bulk change."
            if not latest
            else f"Most recent snapshot {latest.taken_at.isoformat()}."
        ),
    }
