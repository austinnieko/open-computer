"""Stored-path migration (`cptr migrate-paths`) and missing-worktree guards.

Run: uv run python -m unittest tests.test_path_migration -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cptr.utils import path_migration as pm
from cptr.utils.path_migration import Mapping, map_path

BACKUP = "/home/austin/spark-repos/worktree-backup"


class MapPathTests(unittest.TestCase):
    def test_anchored_at_start_and_at_a_path_boundary(self):
        m = [Mapping.parse(f"/home/admin={BACKUP}/home/admin")]
        self.assertEqual(
            map_path("/home/admin/event-recorder", m), f"{BACKUP}/home/admin/event-recorder"
        )
        self.assertEqual(map_path("/home/admin", m), f"{BACKUP}/home/admin")
        self.assertIsNone(map_path("/home/adminx/repo", m))
        self.assertIsNone(map_path("/srv/home/admin/repo", m))  # not at the start

    def test_running_twice_never_rewrites_again(self):
        # The reported bug: the backup path itself contains /home/admin.
        m = [Mapping.parse(f"/home/admin={BACKUP}/home/admin")]
        once = map_path("/home/admin/event-recorder", m)
        self.assertIsNone(map_path(once, m))

    def test_target_inside_source_is_idempotent(self):
        m = [Mapping.parse("/data=/data/v2")]
        self.assertEqual(map_path("/data/x", m), "/data/v2/x")
        self.assertIsNone(map_path("/data/v2/x", m))

    def test_longest_prefix_wins_and_repairs_a_bad_rewrite(self):
        m = [
            Mapping.parse("/home/admin=/home/austin"),
            Mapping.parse(f"{BACKUP}/home/austin={BACKUP}/home/admin"),
        ]
        self.assertEqual(
            map_path(f"{BACKUP}/home/austin/event-recorder", m),
            f"{BACKUP}/home/admin/event-recorder",
        )
        self.assertIsNone(map_path(f"{BACKUP}/home/admin/event-recorder", m))
        self.assertEqual(map_path("/home/admin/x", m), "/home/austin/x")

    def test_bad_mappings_are_rejected(self):
        for bad in ("/a", "a=/b", "/a=b", "/a=/a", "=/b"):
            with self.assertRaises(ValueError):
                Mapping.parse(bad)


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT, data JSON);
        CREATE TABLE chats (id TEXT PRIMARY KEY, meta JSON);
        CREATE TABLE chat_messages (id TEXT PRIMARY KEY, content TEXT);
        CREATE TABLE coord_tasks (id TEXT PRIMARY KEY, repo_key TEXT, repo_root TEXT,
                                  worktree_path TEXT, meta JSON);
        CREATE TABLE coord_claims (id TEXT PRIMARY KEY, resource TEXT);
        """
    )
    old_repo = "/home/admin/event-recorder"
    old_wt = "/home/admin/.cptr-worktrees/event-recorder/fix-bug"
    conn.execute(
        "INSERT INTO workspaces VALUES (?,?,?)",
        ("w1", old_repo, json.dumps({"fileBrowserCwd": old_repo + "/src", "tabs": []})),
    )
    conn.execute(
        "INSERT INTO chats VALUES (?,?)",
        (
            "c1",
            json.dumps(
                {
                    "workspace": old_repo,
                    "cwd": old_wt,
                    "title_hint": "/home/admin/event-recorder is not a path field",
                    "agent_sessions": {"oc": {"workspace": old_wt, "session_id": "ses_1"}},
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO chat_messages VALUES (?,?)", ("m1", "I edited /home/admin/event-recorder/a.py")
    )
    conn.execute(
        "INSERT INTO coord_tasks VALUES (?,?,?,?,?)",
        ("t1", old_repo + "/.git", old_repo, old_wt, json.dumps({})),
    )
    conn.execute("INSERT INTO coord_claims VALUES (?,?)", ("k1", "worktree:" + old_wt))
    conn.execute("INSERT INTO coord_claims VALUES (?,?)", ("k2", "branch:fix-bug"))
    conn.commit()
    conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "app.db")
        _make_db(self.db)
        self.mappings = [Mapping.parse(f"/home/admin={BACKUP}/home/admin")]
        # The new machine's layout.
        self.on_disk = {
            f"{BACKUP}/home/admin/event-recorder",
            f"{BACKUP}/home/admin/event-recorder/src",
            f"{BACKUP}/home/admin/event-recorder/.git",
            f"{BACKUP}/home/admin/.cptr-worktrees/event-recorder/fix-bug",
        }

    def exists(self, path: str) -> bool:
        return path in self.on_disk

    def snapshot(self) -> bytes:
        return Path(self.db).read_bytes()

    def test_dry_run_reports_and_changes_nothing(self):
        before = self.snapshot()
        report = pm.plan(self.db, self.mappings, exists=self.exists)
        self.assertEqual(self.snapshot(), before)
        where = {(c.table, c.column, c.key) for c in report.changes}
        self.assertIn(("workspaces", "path", ""), where)
        self.assertIn(("workspaces", "data", "fileBrowserCwd"), where)
        self.assertIn(("chats", "meta", "cwd"), where)
        self.assertIn(("chats", "meta", "agent_sessions.oc.workspace"), where)
        self.assertIn(("coord_tasks", "worktree_path", ""), where)
        self.assertIn(("coord_claims", "resource", "worktree"), where)
        self.assertFalse(any(c.key == "title_hint" for c in report.changes))
        self.assertTrue(all(c.target_exists for c in report.changes))
        self.assertIn("Dry run", report.summary())

    def test_apply_backs_up_rewrites_known_fields_only_and_is_idempotent(self):
        report = pm.apply(self.db, self.mappings, exists=self.exists)
        self.assertTrue(report.applied)
        self.assertTrue(os.path.exists(report.backup))
        conn = sqlite3.connect(self.db)
        ws_path, ws_data = conn.execute("SELECT path, data FROM workspaces").fetchone()
        self.assertEqual(ws_path, f"{BACKUP}/home/admin/event-recorder")
        self.assertEqual(
            json.loads(ws_data)["fileBrowserCwd"], f"{BACKUP}/home/admin/event-recorder/src"
        )
        meta = json.loads(conn.execute("SELECT meta FROM chats").fetchone()[0])
        self.assertEqual(meta["cwd"], f"{BACKUP}/home/admin/.cptr-worktrees/event-recorder/fix-bug")
        self.assertEqual(meta["agent_sessions"]["oc"]["session_id"], "ses_1")  # preserved
        self.assertEqual(meta["title_hint"], "/home/admin/event-recorder is not a path field")
        content = conn.execute("SELECT content FROM chat_messages").fetchone()[0]
        self.assertEqual(content, "I edited /home/admin/event-recorder/a.py")  # history untouched
        claims = dict(conn.execute("SELECT id, resource FROM coord_claims").fetchall())
        self.assertEqual(
            claims["k1"], f"worktree:{BACKUP}/home/admin/.cptr-worktrees/event-recorder/fix-bug"
        )
        self.assertEqual(claims["k2"], "branch:fix-bug")
        conn.close()

        # The backup still has the original values.
        old = sqlite3.connect(report.backup)
        self.assertEqual(
            old.execute("SELECT path FROM workspaces").fetchone()[0], "/home/admin/event-recorder"
        )
        old.close()

        again = pm.plan(self.db, self.mappings, exists=self.exists)
        self.assertEqual(again.changes, [])

    def test_missing_targets_are_skipped_unless_allowed(self):
        self.on_disk.discard(f"{BACKUP}/home/admin/.cptr-worktrees/event-recorder/fix-bug")
        report = pm.apply(self.db, self.mappings, exists=self.exists)
        conn = sqlite3.connect(self.db)
        wt = conn.execute("SELECT worktree_path FROM coord_tasks").fetchone()[0]
        repo = conn.execute("SELECT repo_root FROM coord_tasks").fetchone()[0]
        conn.close()
        self.assertEqual(wt, "/home/admin/.cptr-worktrees/event-recorder/fix-bug")  # left alone
        self.assertEqual(repo, f"{BACKUP}/home/admin/event-recorder")
        self.assertTrue(report.blocked)
        self.assertIn("new path missing", report.summary())

    def test_check_without_mappings_lists_stale_paths(self):
        report = pm.plan(self.db, [], exists=self.exists)
        self.assertEqual(report.changes, [])
        stale = {m["path"] for m in report.missing}
        self.assertIn("/home/admin/.cptr-worktrees/event-recorder/fix-bug", stale)
        self.assertIn("/home/admin/event-recorder", stale)


if __name__ == "__main__":
    unittest.main()
