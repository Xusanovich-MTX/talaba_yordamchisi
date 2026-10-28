"""Transactional SQLite schema upgrades and legacy owner-ID repair."""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from database.models import Base


logger = logging.getLogger(__name__)
SCHEMA_VERSION = 4
DEFAULT_BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"
OWNER_TABLES = ("schedules", "tasks")
INDEXES = {
    "ix_schedules_user_day_time": (
        "schedules", ("user_id", "day_of_week", "start_time")
    ),
    "ix_tasks_user_completed_deadline": (
        "tasks", ("user_id", "is_completed", "deadline")
    ),
}


class MigrationError(RuntimeError):
    """The database needs manual attention before it can be used safely."""


@dataclass(frozen=True)
class MigrationResult:
    previous_version: int
    version: int
    backup_path: Path | None = None


def _set_sqlite_foreign_keys(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Enable constraint enforcement on every new pooled SQLite connection."""
    if engine.dialect.name == "sqlite" and not event.contains(
        engine.sync_engine, "connect", _set_sqlite_foreign_keys
    ):
        event.listen(engine.sync_engine, "connect", _set_sqlite_foreign_keys)


def _backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / (
        f"{source.stem}.before-v{SCHEMA_VERSION}.{stamp}.{uuid4().hex[:8]}.sqlite3"
    )
    # Reserve a new file without overwriting any previous backup.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError("The pre-migration SQLite backup is corrupt.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


async def _owner_changes(conn: AsyncConnection, tables: set[str]) -> dict:
    if "users" not in tables:
        if set(OWNER_TABLES) & tables:
            raise MigrationError("Missing users table; existing schedules/tasks were not changed.")
        return {}

    users = (await conn.execute(text("SELECT id, telegram_id FROM users"))).all()
    by_id = {row.id: row.id for row in users}
    by_telegram_id = {row.telegram_id: row.id for row in users}
    changes = {}
    errors = []
    for table in OWNER_TABLES:
        if table not in tables:
            continue
        rows = (await conn.execute(text(f"SELECT id, user_id FROM {table}"))).all()
        changes[table] = []
        for row in rows:
            internal_owner = by_id.get(row.user_id)
            telegram_owner = by_telegram_id.get(row.user_id)
            if internal_owner is None and telegram_owner is None:
                errors.append(f"{table}.id={row.id}: user_id={row.user_id} has no user")
            elif (
                internal_owner is not None
                and telegram_owner is not None
                and internal_owner != telegram_owner
            ):
                errors.append(
                    f"{table}.id={row.id}: user_id={row.user_id} is ambiguous "
                    f"(users.id={internal_owner} or users.id={telegram_owner})"
                )
            else:
                owner_id = internal_owner if internal_owner is not None else telegram_owner
                if row.user_id != owner_id:
                    changes[table].append({"record_id": row.id, "owner_id": owner_id})
    if errors:
        raise MigrationError(
            "Owner migration stopped; no changes were committed. "
            "Check the listed records and their intended users: " + "; ".join(errors)
        )
    return changes


async def _upgrade_v1(conn: AsyncConnection, changes: dict) -> None:
    await conn.run_sync(Base.metadata.create_all)
    columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("users"))
    if "username" not in {column["name"] for column in columns}:
        await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN username VARCHAR(32)")

    for table, updates in changes.items():
        if updates:
            await conn.execute(
                text(f"UPDATE {table} SET user_id = :owner_id WHERE id = :record_id"),
                updates,
            )

    for name, (table, columns) in INDEXES.items():
        await conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)})"
        )


async def _upgrade_v2(conn: AsyncConnection) -> None:
    columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("users"))
    existing = {column["name"] for column in columns}
    for name, sql_type in (
        ("first_name", "VARCHAR(50)"),
        ("last_name", "VARCHAR(50)"),
        ("phone", "VARCHAR(32)"),
    ):
        if name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {name} {sql_type}")


async def _upgrade_v3(conn: AsyncConnection) -> None:
    columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("users"))
    existing = {column["name"] for column in columns}
    for name, sql_type in (
        ("hemis_institution_code", "VARCHAR(20)"),
        ("hemis_institution_name", "VARCHAR(300)"),
        ("hemis_api_url", "VARCHAR(512)"),
        ("hemis_student_url", "VARCHAR(512)"),
    ):
        if name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {name} {sql_type}")


async def _upgrade_v4(conn: AsyncConnection) -> None:
    columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("users"))
    if "hemis_session_ciphertext" not in {column["name"] for column in columns}:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN hemis_session_ciphertext TEXT"
        )


async def _verify_database(conn: AsyncConnection) -> None:
    if (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() != 1:
        raise MigrationError("SQLite foreign-key enforcement could not be enabled.")
    tables = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
    missing = {"users", *OWNER_TABLES} - tables
    if missing:
        raise MigrationError(f"Database schema is missing tables: {', '.join(sorted(missing))}")

    user_columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("users"))
    if "username" not in {column["name"] for column in user_columns}:
        raise MigrationError("Database schema is missing users.username.")
    missing_profile_columns = {"first_name", "last_name", "phone"} - {
        column["name"] for column in user_columns
    }
    if missing_profile_columns:
        raise MigrationError(
            "Database schema is missing users columns: "
            + ", ".join(sorted(missing_profile_columns))
        )
    missing_hemis_columns = {
        "hemis_institution_code",
        "hemis_institution_name",
        "hemis_api_url",
        "hemis_student_url",
    } - {column["name"] for column in user_columns}
    if missing_hemis_columns:
        raise MigrationError(
            "Database schema is missing users columns: "
            + ", ".join(sorted(missing_hemis_columns))
        )
    if "hemis_session_ciphertext" not in {column["name"] for column in user_columns}:
        raise MigrationError("Database schema is missing users.hemis_session_ciphertext.")

    for table in OWNER_TABLES:
        foreign_keys = await conn.run_sync(
            lambda sync, table=table: inspect(sync).get_foreign_keys(table)
        )
        if not any(
            fk["constrained_columns"] == ["user_id"]
            and fk["referred_table"] == "users"
            and fk["referred_columns"] == ["id"]
            for fk in foreign_keys
        ):
            raise MigrationError(f"{table}.user_id must reference users.id.")

    violations = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
    if violations:
        details = "; ".join(
            f"{row[0]}.rowid={row[1]} references missing {row[2]}" for row in violations
        )
        raise MigrationError("Database foreign-key check failed: " + details)

    for name, (table, columns) in INDEXES.items():
        indexes = await conn.run_sync(
            lambda sync, table=table: inspect(sync).get_indexes(table)
        )
        if not any(
            index["name"] == name and tuple(index["column_names"]) == columns
            for index in indexes
        ):
            raise MigrationError(f"Database schema is missing the expected index {name}.")


async def migrate_database(
    engine: AsyncEngine, backup_dir: Path | None = None
) -> MigrationResult:
    """Upgrade before starting handlers; leave ambiguous legacy ownership untouched.

    Existing file databases are backed up with SQLite's online backup API while a
    write reservation prevents concurrent changes. In-memory databases need no
    file backup. user_version, DDL and both owner remaps commit together.
    """
    if engine.dialect.name != "sqlite":
        raise MigrationError("This migration supports SQLite databases only.")
    enable_sqlite_foreign_keys(engine)
    async with engine.connect() as conn:
        # Also cover connections pooled before the listener was installed.
        await conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        backup_path = None
        try:
            version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar_one()
            if version > SCHEMA_VERSION:
                raise MigrationError(
                    f"Database version {version} is newer than supported version {SCHEMA_VERSION}."
                )
            if version in (0, 1, 2, 3):
                tables = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
                changes = await _owner_changes(conn, tables) if version == 0 else {}
                db_path = (await conn.exec_driver_sql("PRAGMA database_list")).all()[0][2]
                if tables and db_path:
                    backup_path = await asyncio.to_thread(
                        _backup_database,
                        Path(db_path).resolve(),
                        Path(backup_dir) if backup_dir is not None else DEFAULT_BACKUP_DIR,
                    )
                    logger.info("Pre-migration SQLite backup: %s", backup_path)
                if version == 0:
                    await _upgrade_v1(conn, changes)
                if version <= 1:
                    await _upgrade_v2(conn)
                if version <= 2:
                    await _upgrade_v3(conn)
                await _upgrade_v4(conn)
                await _verify_database(conn)
                await conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                await _verify_database(conn)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    return MigrationResult(version, SCHEMA_VERSION, backup_path)
