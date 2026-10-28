"""Migration tests use only disposable SQLite databases, never the app database."""

import sqlite3
import shutil
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.migrations import MigrationError, migrate_database


LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    timezone VARCHAR(50) NOT NULL,
    notify_class BOOLEAN NOT NULL,
    notify_deadline BOOLEAN NOT NULL
);
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    subject VARCHAR(100) NOT NULL,
    day_of_week INTEGER NOT NULL,
    start_time TIME NOT NULL,
    room_or_link VARCHAR(200)
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    subject VARCHAR(100) NOT NULL,
    description VARCHAR(500) NOT NULL,
    deadline DATETIME NOT NULL,
    is_completed BOOLEAN NOT NULL
);
"""


def seed_legacy(path, *, schedule_owner=10001, task_owner=10002, ambiguous=False):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 2 if ambiguous else 10001, "Ali <&>", "Asia/Tashkent", 0, 1),
                (2, 10002, "Vali", "Asia/Samarkand", 1, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO schedules VALUES (?, ?, ?, ?, ?, ?)",
            [
                (11, schedule_owner, "Matematika", 1, "09:30:00", None),
                (12, 2, "Fizika", 7, "23:50:00", "https://example.org/?a=1&b=2"),
            ],
        )
        conn.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            [
                (21, task_owner, "Fizika", "5-masala <&>", "2026-11-01 10:00:00", 0),
                (22, 1, "Tarix", "Tayyor", "2026-10-01 17:00:00", 1),
            ],
        )
        conn.commit()


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return {
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
            "schema": conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "users": conn.execute("SELECT * FROM users ORDER BY id").fetchall(),
            "schedules": conn.execute("SELECT * FROM schedules ORDER BY id").fetchall(),
            "tasks": conn.execute("SELECT * FROM tasks ORDER BY id").fetchall(),
        }


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = Path(__file__).resolve().parent
        # Python 3.13's restrictive temporary-directory ACL is incompatible with
        # the Windows execution sandbox. Inherit the workspace directory's ACL.
        self.root = self.test_dir / f".migration-test-{uuid4().hex}"
        self.root.mkdir()
        self.path = self.root / "student.sqlite3"
        self.backups = self.root / "backups"
        self.engines = []

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.dispose()
        self.assertTrue(self.root.resolve().is_relative_to(self.test_dir))
        shutil.rmtree(self.root)

    def engine(self, memory=False):
        url = "sqlite+aiosqlite:///:memory:" if memory else f"sqlite+aiosqlite:///{self.path.as_posix()}"
        engine = create_async_engine(url)
        self.engines.append(engine)
        return engine

    async def test_mixed_legacy_and_internal_ids_preserve_all_content(self):
        seed_legacy(self.path)
        before = snapshot(self.path)
        result = await migrate_database(self.engine(), self.backups)
        after = snapshot(self.path)

        self.assertEqual((result.previous_version, result.version), (0, 4))
        self.assertEqual(after["version"], 4)
        self.assertEqual([row[1] for row in after["schedules"]], [1, 2])
        self.assertEqual([row[1] for row in after["tasks"]], [2, 1])
        self.assertEqual([row[:-9] for row in after["users"]], before["users"])
        self.assertTrue(all(row[-9:] == (None,) * 9 for row in after["users"]))
        for table in ("schedules", "tasks"):
            for old, new in zip(before[table], after[table]):
                self.assertEqual(old[0], new[0])
                self.assertEqual(old[2:], new[2:])
        self.assertEqual(snapshot(result.backup_path), before)
        with closing(sqlite3.connect(result.backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    async def test_repeat_is_idempotent_and_does_not_create_another_backup(self):
        seed_legacy(self.path)
        engine = self.engine()
        first = await migrate_database(engine, self.backups)
        before = snapshot(self.path)

        repeated = await migrate_database(engine, self.backups)

        self.assertEqual((repeated.previous_version, repeated.version), (4, 4))
        self.assertIsNone(repeated.backup_path)
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual(list(self.backups.iterdir()), [first.backup_path])

    async def test_fresh_file_and_memory_databases_initialize(self):
        for memory in (False, True):
            with self.subTest(memory=memory):
                engine = self.engine(memory=memory)
                result = await migrate_database(engine, self.backups)
                self.assertIsNone(result.backup_path)
                async with engine.connect() as conn:
                    self.assertEqual((await conn.exec_driver_sql("PRAGMA user_version")).scalar_one(), 4)
                    self.assertEqual((await conn.exec_driver_sql("PRAGMA foreign_key_check")).all(), [])
                    columns = (await conn.exec_driver_sql("PRAGMA table_info(users)")).all()
                    self.assertTrue({
                        "username", "first_name", "last_name", "phone",
                        "hemis_institution_code", "hemis_institution_name",
                        "hemis_api_url", "hemis_student_url", "hemis_session_ciphertext",
                    }.issubset({column[1] for column in columns}))
                    indexes = (await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )).scalars().all()
                    self.assertIn("ix_schedules_user_day_time", indexes)
                    self.assertIn("ix_tasks_user_completed_deadline", indexes)

    async def test_ambiguous_owner_aborts_without_changing_anything(self):
        seed_legacy(self.path, schedule_owner=2, ambiguous=True)
        before = snapshot(self.path)

        with self.assertRaisesRegex(MigrationError, r"schedules.id=11: user_id=2 is ambiguous"):
            await migrate_database(self.engine(), self.backups)

        self.assertEqual(snapshot(self.path), before)

    async def test_missing_owner_aborts_both_table_migrations(self):
        seed_legacy(self.path, task_owner=987654)
        before = snapshot(self.path)

        with self.assertRaisesRegex(MigrationError, r"tasks.id=21: user_id=987654 has no user"):
            await migrate_database(self.engine(), self.backups)

        self.assertEqual(snapshot(self.path), before)

    async def test_failure_after_updates_rolls_back_data_ddl_and_version(self):
        seed_legacy(self.path)
        before = snapshot(self.path)

        with patch(
            "database.migrations._verify_database",
            new=AsyncMock(side_effect=MigrationError("verification failed")),
        ):
            with self.assertRaisesRegex(MigrationError, "verification failed"):
                await migrate_database(self.engine(), self.backups)

        self.assertEqual(snapshot(self.path), before)
        backup_paths = list(self.backups.iterdir())
        self.assertEqual(len(backup_paths), 1)
        self.assertEqual(snapshot(backup_paths[0]), before)

    async def test_foreign_keys_are_enforced_on_every_connection(self):
        engine = self.engine()
        await migrate_database(engine, self.backups)

        async with engine.connect() as first:
            async with engine.connect() as second:
                for conn in (first, second):
                    self.assertEqual((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one(), 1)
                    with self.assertRaises(IntegrityError):
                        await conn.exec_driver_sql(
                            "INSERT INTO schedules (user_id, subject, day_of_week, start_time) "
                            "VALUES (999, 'Invalid owner', 1, '10:00:00')"
                        )
                    await conn.rollback()

    async def test_current_version_is_verified_without_reinterpreting_internal_ids(self):
        seed_legacy(self.path)
        engine = self.engine()
        await migrate_database(engine, self.backups)
        async with engine.begin() as conn:
            await conn.exec_driver_sql("UPDATE users SET telegram_id=2 WHERE id=1")

        # Internal user_id=2 remains user 2 even if another Telegram ID is now 2.
        await migrate_database(engine, self.backups)
        self.assertEqual([row[1] for row in snapshot(self.path)["tasks"]], [2, 1])

    async def test_current_version_rejects_foreign_key_corruption(self):
        engine = self.engine()
        await migrate_database(engine, self.backups)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "INSERT INTO tasks (user_id, subject, description, deadline, is_completed) "
                "VALUES (999, 'Orphan', 'Bad owner', '2026-11-01 10:00:00', 0)"
            )
            conn.commit()

        with self.assertRaisesRegex(MigrationError, r"tasks.rowid=1 references missing users"):
            await migrate_database(engine, self.backups)


if __name__ == "__main__":
    unittest.main()
