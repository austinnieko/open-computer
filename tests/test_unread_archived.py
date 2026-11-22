"""The workspace unread badge ignores archived chats (and counts them again once restored)."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import insert

import cptr.utils.db as db
from cptr.models.chats import Chat

USER = "user-1"
WS = "/work/repo"


class UnreadArchivedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_file = Path(os.path.realpath(self._tmp.name)) / "app.db"
        db.DB_FILE = db_file
        db._engine = None
        db._async_session = None
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(Path(db.__file__).parent.parent / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
        command.upgrade(cfg, "head")

    async def asyncTearDown(self):
        if db._engine is not None:
            await db._engine.dispose()
        db._engine = None
        db._async_session = None

    def tearDown(self):
        self._tmp.cleanup()

    async def add_chat(self, meta, updated_at=2000, last_read_at=1000):
        chat_id = str(uuid.uuid4())
        async with await db.get_db() as s:
            await s.execute(insert(Chat).values(id=chat_id, user_id=USER, title="t", meta={"workspace": WS, **meta},
                                                created_at=1, updated_at=updated_at, last_read_at=last_read_at))
            await s.commit()
        return chat_id

    async def count(self, active=frozenset()):
        return (await Chat.unread_counts_by_workspace(USER, [WS], set(active))).get(WS, 0)

    async def test_archived_unread_chats_do_not_count(self):
        await self.add_chat({})
        archived = await self.add_chat({"archived": True})
        await self.add_chat({"subagent": True})
        await self.add_chat({}, updated_at=500, last_read_at=1000)  # read
        self.assertEqual(await self.count(), 1)
        await Chat.set_archived(archived, False)
        self.assertEqual(await self.count(), 2)

    async def test_archiving_an_unread_chat_clears_it_from_the_badge(self):
        chat = await self.add_chat({})
        self.assertEqual(await self.count(), 1)
        await Chat.set_archived(chat, True)
        self.assertEqual(await self.count(), 0)

    async def test_active_chats_still_excluded(self):
        chat = await self.add_chat({})
        self.assertEqual(await self.count({chat}), 0)


if __name__ == "__main__":
    unittest.main()
