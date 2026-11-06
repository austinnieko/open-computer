"""Move stored workspace/worktree paths to a new location, safely.

Used after moving Open Computer to another machine or home directory (e.g.
Spark ``/home/admin/...`` → x86server ``/home/austin/spark-repos/...``).

Rules, each one there because a naive rewrite went wrong before:

* **Explicit mappings only.** ``OLD=NEW`` prefixes given by the user; nothing
  is guessed.
* **Anchored at the start, at a path boundary.** ``/home/admin`` matches
  ``/home/admin`` and ``/home/admin/x``, never ``/home/adminx`` or a
  ``/home/admin`` in the middle of a path. (A substring replace is what
  turned ``.../worktree-backup/home/admin/event-recorder`` into
  ``.../worktree-backup/home/austin/event-recorder``.)
* **Applied once, longest prefix wins, idempotent.** A value already under a
  mapping's target is left alone, so running the migration twice, or with a
  target that contains its source, can't rewrite a path again.
* **Only known path fields** in known tables (and known keys inside their
  JSON), never chat message text or arbitrary strings.
* **The target must exist** unless ``allow_missing`` is set; values whose
  target is missing are reported, not changed.
* **Dry run by default**, with a report; applying backs the database up
  first and writes everything in one transaction. Files and worktrees on
  disk are never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# (table, column) holding a plain absolute path.
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("workspaces", "path"),
    ("automations", "workspace"),
    ("coord_tasks", "repo_root"),
    ("coord_tasks", "worktree_path"),
    ("coord_tasks", "repo_key"),
    ("coord_integrations", "worktree_path"),
    ("coord_integrations", "repo_key"),
)
# (table, column) holding a "<kind>:<abs path>" resource string.
RESOURCE_FIELDS: tuple[tuple[str, str], ...] = (("coord_claims", "resource"),)
# (table, column) holding JSON; only these keys (at any depth) are paths.
JSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("workspaces", "data"),
    ("chats", "meta"),
    ("coord_tasks", "meta"),
)
JSON_PATH_KEYS = frozenset(
    {"workspace", "cwd", "path", "fileBrowserCwd", "worktree_path", "repo_root", "root"}
)


@dataclass(frozen=True)
class Mapping:
    old: str
    new: str

    @staticmethod
    def parse(text: str) -> "Mapping":
        old, sep, new = text.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise ValueError(f"Mapping must be OLD=NEW, got {text!r}")
        old, new = _norm(old.strip()), _norm(new.strip())
        if not os.path.isabs(old) or not os.path.isabs(new):
            raise ValueError(f"Mapping paths must be absolute: {text!r}")
        if old == new:
            raise ValueError(f"Mapping does nothing: {text!r}")
        return Mapping(old, new)


def _norm(path: str) -> str:
    return path.rstrip("/") or "/"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def map_path(path: str, mappings: Iterable[Mapping]) -> str | None:
    """The migrated path, or None when no mapping applies (or it already did)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    ordered = sorted(mappings, key=lambda m: len(m.old), reverse=True)
    for m in ordered:
        if not _under(path, m.old):
            continue
        # When a target lies under its own source (/data → /data/v2), a value
        # already under the target was migrated before: don't apply it twice.
        if _under(path, m.new):
            return None
        return m.new + path[len(m.old.rstrip("/")) :] if m.old != "/" else m.new + path
    return None


@dataclass
class Change:
    table: str
    column: str
    row_id: str
    key: str  # JSON key path, "" for a plain column
    old: str
    new: str
    target_exists: bool
    old_exists: bool

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Report:
    db: str
    mappings: list[Mapping]
    changes: list[Change] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)  # unmapped and not on disk
    applied: bool = False
    backup: str | None = None

    @property
    def to_apply(self) -> list[Change]:
        return [c for c in self.changes if c.target_exists]

    @property
    def blocked(self) -> list[Change]:
        return [c for c in self.changes if not c.target_exists]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db": self.db,
            "mappings": [m.__dict__ for m in self.mappings],
            "applied": self.applied,
            "backup": self.backup,
            "changes": [c.to_dict() for c in self.changes],
            "missing": self.missing,
        }

    def summary(self, allow_missing: bool = False) -> str:
        lines = [f"Database: {self.db}"]
        lines += [f"Mapping:  {m.old}  →  {m.new}" for m in self.mappings]
        apply = self.changes if allow_missing else self.to_apply
        lines.append("")
        lines.append(
            f"{len(apply)} value(s) {'updated' if self.applied else 'would be updated'}"
            + (
                f", {len(self.blocked)} skipped because the new path doesn't exist"
                if self.blocked and not allow_missing
                else ""
            )
        )
        for c in self.changes:
            mark = "✓" if c.target_exists else ("!" if allow_missing else "✗ (new path missing)")
            where = f"{c.table}.{c.column}" + (f"[{c.key}]" if c.key else "")
            lines.append(f"  {mark} {where} {c.row_id}\n      {c.old}\n    → {c.new}")
        if self.missing:
            lines.append("")
            lines.append(
                f"{len(self.missing)} stored path(s) don't exist and no mapping covers them:"
            )
            by_prefix: dict[str, int] = {}
            for item in self.missing:
                parent = os.path.dirname(item["path"])
                by_prefix[parent] = by_prefix.get(parent, 0) + 1
            for parent, count in sorted(by_prefix.items(), key=lambda kv: -kv[1])[:20]:
                lines.append(f"  {count:4d} under {parent}")
        if self.backup:
            lines.append("")
            lines.append(f"Backup: {self.backup}")
        if not self.applied and apply:
            lines.append("")
            lines.append("Dry run: nothing changed. Re-run with --apply to write these changes.")
        return "\n".join(lines)


def _tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
    return out


def _pk(columns: set[str]) -> str:
    return "id" if "id" in columns else "rowid"


def _walk_json(
    value: Any, visit: Callable[[str, str], str | None], path: str = ""
) -> tuple[Any, bool]:
    """Rewrite string values under JSON_PATH_KEYS; returns (value, changed)."""
    changed = False
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key_path = f"{path}.{k}" if path else str(k)
            if k in JSON_PATH_KEYS and isinstance(v, str):
                new = visit(key_path, v)
                if new is not None:
                    out[k] = new
                    changed = True
                    continue
            nv, c = _walk_json(v, visit, key_path)
            out[k] = nv
            changed |= c
        return out, changed
    if isinstance(value, list):
        out_list = []
        for i, v in enumerate(value):
            nv, c = _walk_json(v, visit, f"{path}[{i}]")
            out_list.append(nv)
            changed |= c
        return out_list, changed
    return value, False


def plan(
    db_path: str | Path,
    mappings: list[Mapping],
    *,
    exists: Callable[[str], bool] = os.path.exists,
) -> Report:
    """Read-only: what a migration would change, and what's missing on disk."""
    report = Report(db=str(db_path), mappings=list(mappings))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = _tables(conn)

        def consider(table, column, row_id, key, value):
            if not isinstance(value, str) or not value.startswith("/"):
                return None
            new = map_path(value, mappings)
            if new is None:
                if not exists(value):
                    report.missing.append(
                        {"table": table, "column": column, "id": row_id, "key": key, "path": value}
                    )
                return None
            report.changes.append(
                Change(table, column, row_id, key, value, new, exists(new), exists(value))
            )
            return new

        for table, column in TEXT_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            for row_id, value in conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"'):
                consider(table, column, str(row_id), "", value)

        for table, column in RESOURCE_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            for row_id, value in conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"'):
                if isinstance(value, str) and ":/" in value:
                    kind, _, rest = value.partition(":")
                    consider(table, column, str(row_id), kind, "/" + rest.lstrip("/"))

        for table, column in JSON_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            for row_id, raw in conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"'):
                try:
                    data = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    continue
                if data is None:
                    continue
                _walk_json(data, lambda k, v: consider(table, column, str(row_id), k, v))
    finally:
        conn.close()
    return report


def backup_database(db_path: str | Path) -> str:
    """Consistent copy (sqlite online backup, safe while the app runs)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{db_path}.bak-{stamp}"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def apply(
    db_path: str | Path,
    mappings: list[Mapping],
    *,
    allow_missing: bool = False,
    exists: Callable[[str], bool] = os.path.exists,
) -> Report:
    """Back up, then write the planned changes in one transaction."""
    report = plan(db_path, mappings, exists=exists)
    wanted = report.changes if allow_missing else report.to_apply
    if not wanted:
        return report
    allowed = {(c.table, c.column, c.row_id, c.key, c.old) for c in wanted}

    def ok(table, column, row_id, key, value) -> str | None:
        new = map_path(value, mappings)
        if new is None or (table, column, row_id, key, value) not in allowed:
            return None
        return new

    report.backup = backup_database(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = _tables(conn)
        for table, column in TEXT_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            rows = conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"').fetchall()
            for row_id, value in rows:
                if isinstance(value, str) and (new := ok(table, column, str(row_id), "", value)):
                    conn.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE {pk} = ?', (new, row_id))
        for table, column in RESOURCE_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            rows = conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"').fetchall()
            for row_id, value in rows:
                if not (isinstance(value, str) and ":/" in value):
                    continue
                kind, _, rest = value.partition(":")
                path = "/" + rest.lstrip("/")
                if new := ok(table, column, str(row_id), kind, path):
                    conn.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE {pk} = ?',
                        (f"{kind}:{new}", row_id),
                    )
        for table, column in JSON_FIELDS:
            if column not in tables.get(table, ()):
                continue
            pk = _pk(tables[table])
            rows = conn.execute(f'SELECT {pk}, "{column}" FROM "{table}"').fetchall()
            for row_id, raw in rows:
                try:
                    data = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    continue
                if data is None:
                    continue
                new_data, changed = _walk_json(
                    data, lambda k, v: ok(table, column, str(row_id), k, v)
                )
                if changed:
                    conn.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE {pk} = ?',
                        (json.dumps(new_data), row_id),
                    )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    report.applied = True
    return report
