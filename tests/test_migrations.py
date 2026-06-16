"""
Tests for the ASF Alembic schema migration.

These tests verify the contract of the initial migration
(``0001_initial_schema``) against the assertions in
``validation-contract.md`` for milestones M2 (database migrations and
schema). Each test maps to one or more validation IDs:

* ``test_all_tables_present``        — VAL-API-059
* ``test_each_table_has_timestamps`` — VAL-API-059
* ``test_foreign_keys_cascade``      — VAL-API-060
* ``test_enum_columns_exist``        — VAL-API-061
* ``test_downgrade_reverts_schema``  — VAL-API-062
* ``test_mission_status_index``      — VAL-API-063
* ``test_mission_created_at_index``  — VAL-API-063
* ``test_jsonb_columns``             — VAL-API-064
* ``test_chat_sender_type_enum``     — VAL-API-065
* ``test_chat_sender_id_nullable``   — VAL-API-065
* ``test_reupgrade_is_noop``         — VAL-CROSS-013
* ``test_version_table_tracks_state``— VAL-CROSS-013

The tests run against a real PostgreSQL database when
``ASF_TEST_DATABASE_URL`` is set (recommended — the PG-specific bits
like native ENUM types and JSONB only materialise on a real backend).
When the env var is unset, the tests are skipped (so a CI without
Postgres can still run the rest of the suite).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

# Skip the entire module when no PostgreSQL test database is configured.
# The tests are intentionally PG-only: they validate the native ENUM
# types, JSONB columns, and CHECK constraints that the ASF schema
# requires. SQLite-backed models use generic JSON/VARCHAR fallbacks.
_TEST_DB_URL = os.getenv("ASF_TEST_DATABASE_URL", "").strip()


pytestmark = pytest.mark.skipif(
    not _TEST_DB_URL,
    reason=(
        "ASF_TEST_DATABASE_URL is not set; PostgreSQL is required to "
        "verify the ASF schema (native ENUMs + JSONB)."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def db_url() -> str:
    """Return the PostgreSQL test database URL."""
    return _TEST_DB_URL


@pytest.fixture(scope="module")
def migrated_engine(db_url):
    """Yield a SQLAlchemy engine bound to a freshly migrated database.

    Creates a one-off database (or uses the existing one if
    ``ASF_TEST_DATABASE_URL`` already points at it) so that re-running
    the test suite is safe. The database is dropped on teardown.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Engine

    from soy.db import run_alembic_upgrade

    # If the URL points at a database that already has the schema,
    # ``alembic upgrade head`` is a no-op. We use the URL as-is.
    engine: Engine = create_engine(db_url, future=True)
    # Sanity: the connection must work before we run migrations.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    run_alembic_upgrade(database_url=db_url)
    try:
        yield engine
    finally:
        # Drop the schema by running downgrade to base. This keeps the
        # external database clean for the next run.
        try:
            from alembic import command
            from alembic.config import Config

            from soy.db import _locate_alembic_ini

            cfg = Config(_locate_alembic_ini())
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.downgrade(cfg, "base")
        except Exception:
            pass  # best-effort cleanup
        engine.dispose()


@contextmanager
def session_scope(engine) -> Iterator:
    """Yield a SQLAlchemy session, commit on success, rollback on error."""
    from sqlalchemy.orm import Session

    with Session(engine, future=True) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _expected_tables() -> set[str]:
    return {
        "missions", "agents", "tasks", "executions",
        "approvals", "chat_messages",
    }


def _expected_status_enums() -> set[str]:
    """Names of ENUM types that should exist for status columns."""
    return {
        "mission_status",
        "agent_role",
        "agent_status",
        "task_status",
        "execution_status",
        "approval_gate_type",
        "approval_decision",
        "chat_sender_type",
    }


# ---------------------------------------------------------------------------
# Tests — VAL-API-059
# ---------------------------------------------------------------------------
class TestTablesExist:
    """VAL-API-059: every domain table exists with PK + timestamps."""

    def test_all_tables_present(self, migrated_engine):
        from sqlalchemy import text

        expected = _expected_tables()
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                )
            ).fetchall()
        actual = {r[0] for r in rows}
        missing = expected - actual
        assert not missing, f"Missing tables: {missing}"

    def test_each_table_has_primary_key(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            for table in _expected_tables():
                row = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.table_constraints "
                        "WHERE table_schema='public' AND table_name=:t "
                        "AND constraint_type='PRIMARY KEY'"
                    ),
                    {"t": table},
                ).scalar()
                assert row == 1, f"Table {table!r} is missing a PRIMARY KEY"

    def test_each_table_has_timestamps(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            for table in _expected_tables():
                rows = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t "
                        "AND column_name IN ('created_at', 'updated_at')"
                    ),
                    {"t": table},
                ).fetchall()
                cols = {r[0] for r in rows}
                assert {"created_at", "updated_at"} <= cols, (
                    f"Table {table!r} missing timestamps; got {cols}"
                )

    def test_alembic_version_at_head(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).fetchone()
        assert row is not None
        assert row[0] == "0001_initial_schema"


# ---------------------------------------------------------------------------
# Tests — VAL-API-060
# ---------------------------------------------------------------------------
class TestForeignKeys:
    """VAL-API-060: FK constraints enforce referential integrity and cascade."""

    def test_agents_have_fk_to_missions(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint "
                    "WHERE conrelid='agents'::regclass AND contype='f'"
                )
            ).fetchall()
        fk_defs = " ".join(r[1] for r in rows)
        assert "missions(id)" in fk_defs
        assert "ON DELETE CASCADE" in fk_defs

    def test_tasks_have_fk_to_missions_and_agents(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='tasks'::regclass AND contype='f'"
                )
            ).fetchall()
        fk_defs = " ".join(r[0] for r in rows)
        assert "missions(id)" in fk_defs
        assert "agents(id)" in fk_defs

    def test_executions_have_fk_to_tasks_agents_missions(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='executions'::regclass AND contype='f'"
                )
            ).fetchall()
        fk_defs = " ".join(r[0] for r in rows)
        for ref in ("tasks(id)", "agents(id)", "missions(id)"):
            assert ref in fk_defs, f"Missing FK to {ref} on executions"

    def test_approvals_chat_have_fk_to_missions(self, migrated_engine):
        from sqlalchemy import text

        # The table names are hard-coded in the test, so we can safely
        # interpolate them as SQL identifiers (they are not user
        # input). We then look up the FK constraints on each OID.
        with migrated_engine.connect() as conn:
            for table in ("approvals", "chat_messages"):
                rows = conn.execute(
                    text(
                        f"SELECT pg_get_constraintdef(oid) "
                        f"FROM pg_constraint "
                        f"WHERE conrelid = '{table}'::regclass "
                        f"AND contype='f'"
                    )
                ).fetchall()
                fk_defs = " ".join(r[0] for r in rows)
                assert "missions(id)" in fk_defs, (
                    f"Table {table} missing FK to missions; "
                    f"got constraints: {fk_defs}"
                )

    def test_inserting_task_with_bad_mission_fails(self, migrated_engine):
        """VAL-API-060: inserting a row with a non-existent FK raises."""
        from sqlalchemy.exc import IntegrityError

        # Use SQLAlchemy ORM insert to get correct UUID handling. The
        # test relies on the FK constraint to reject the row, not on
        # the column type coercion.
        import soy.models as models
        from sqlalchemy.orm import Session

        bad_mission = uuid.uuid4()
        bad_agent = uuid.uuid4()
        with Session(migrated_engine, future=True) as session:
            with pytest.raises(IntegrityError):
                with session.begin():
                    task = models.Task(
                        mission_id=bad_mission,
                        agent_id=bad_agent,
                        description="x",
                    )
                    session.add(task)

    def test_deleting_mission_cascades(self, migrated_engine):
        """VAL-API-060: deleting a mission removes its children."""
        import soy.models as models
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        # Insert a mission + an agent + a task + a chat message
        # and verify that a DELETE on the mission removes everything.
        with Session(migrated_engine, future=True) as session:
            with session.begin():
                mission = models.Mission(title="cascade test")
                session.add(mission)
                session.flush()
                agent = models.Agent(
                    mission_id=mission.id, name="a1", role=models.AgentRole.coder
                )
                session.add(agent)
                session.flush()
                task = models.Task(
                    mission_id=mission.id,
                    agent_id=agent.id,
                    description="d",
                )
                session.add(task)
                chat = models.ChatMessage(
                    mission_id=mission.id,
                    sender_type=models.ChatSenderType.user,
                    content="hi",
                )
                session.add(chat)
                session.flush()
                mission_id = mission.id
                agent_id = agent.id
                task_id = task.id
                chat_id = chat.id

        with migrated_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text("DELETE FROM missions WHERE id = :id"),
                    {"id": str(mission_id)},
                )

        with migrated_engine.connect() as conn:
            for table, ids in (
                ("agents", [agent_id]),
                ("tasks", [task_id]),
                ("chat_messages", [chat_id]),
            ):
                # Cast each UUID string in the list to uuid type so
                # PostgreSQL can compare against the uuid column. The
                # table names are hard-coded so direct interpolation
                # is safe.
                uuid_list_sql = "ARRAY[" + ",".join(
                    f"'{i}'::uuid" for i in ids
                ) + "]"
                rows = conn.execute(
                    text(f"SELECT id FROM {table} WHERE id = ANY({uuid_list_sql})")
                ).fetchall()
                assert not rows, (
                    f"Expected {table} rows to be cascaded away, got {rows}"
                )


# ---------------------------------------------------------------------------
# Tests — VAL-API-061
# ---------------------------------------------------------------------------
class TestEnumColumns:
    """VAL-API-061: ENUM types or CHECK constraints restrict status columns."""

    def test_status_enum_types_exist(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT typname FROM pg_type "
                    "WHERE typtype='e' AND typname LIKE '%_status' "
                    "OR typname IN ('agent_role', 'approval_gate_type', "
                    "'approval_decision', 'chat_sender_type')"
                )
            ).fetchall()
        actual = {r[0] for r in rows}
        for name in _expected_status_enums():
            assert name in actual, f"Missing enum type {name!r}"

    def test_mission_status_is_pg_enum(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name='missions' AND column_name='status'"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "mission_status", (
            f"missions.status should be mission_status, got {row[0]!r}"
        )

    def test_invalid_mission_status_rejected(self, migrated_engine):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, IntegrityError

        with migrated_engine.connect() as conn:
            with conn.begin():
                with pytest.raises((IntegrityError, DBAPIError)):
                    conn.execute(
                        text(
                            "INSERT INTO missions (id, title, status) "
                            "VALUES (gen_random_uuid()::text, 'x', 'bogus')"
                        )
                    )

    def test_invalid_agent_role_rejected(self, migrated_engine):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, IntegrityError

        mission_id = str(uuid.uuid4())
        with migrated_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO missions (id, title, status) "
                        "VALUES (:id, 'x', 'created')"
                    ),
                    {"id": mission_id},
                )
                with pytest.raises((IntegrityError, DBAPIError)):
                    conn.execute(
                        text(
                            "INSERT INTO agents (id, mission_id, name, role, status) "
                            "VALUES (gen_random_uuid()::text, :m, 'a', 'hacker', 'idle')"
                        ),
                        {"m": mission_id},
                    )


# ---------------------------------------------------------------------------
# Tests — VAL-API-062
# ---------------------------------------------------------------------------
class TestMigrationRollback:
    """VAL-API-062: alembic downgrade -1 reverts the latest migration."""

    def test_downgrade_removes_tables(self, db_url):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text

        from soy.db import _locate_alembic_ini, run_alembic_upgrade

        # Upgrade first to ensure we are at head.
        run_alembic_upgrade(database_url=db_url)
        engine = create_engine(db_url, future=True)
        with engine.connect() as conn:
            before = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE'"
                    )
                ).fetchall()
            }
        assert {"missions", "agents", "tasks"} <= before

        # Downgrade to base — should drop every ASF table.
        cfg = Config(_locate_alembic_ini())
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.downgrade(cfg, "base")

        with engine.connect() as conn:
            after = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE'"
                    )
                ).fetchall()
            }
        # Only alembic_version should remain
        assert "missions" not in after
        assert "agents" not in after
        assert "tasks" not in after
        assert "executions" not in after
        assert "approvals" not in after
        assert "chat_messages" not in after
        assert after == {"alembic_version"}

        # Re-upgrade so other tests in the session still see a
        # populated schema.
        command.upgrade(cfg, "head")
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests — VAL-API-063
# ---------------------------------------------------------------------------
class TestIndexes:
    """VAL-API-063: btree indexes on missions(status) and missions(created_at)."""

    def test_mission_status_index(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND tablename='missions' "
                    "AND indexname='ix_missions_status'"
                )
            ).fetchone()
        assert row is not None, "ix_missions_status is missing"
        assert "btree" in row[0]
        assert "(status)" in row[0]

    def test_mission_created_at_index(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND tablename='missions' "
                    "AND indexname='ix_missions_created_at'"
                )
            ).fetchone()
        assert row is not None, "ix_missions_created_at is missing"
        assert "btree" in row[0]
        assert "(created_at)" in row[0]


# ---------------------------------------------------------------------------
# Tests — VAL-API-064
# ---------------------------------------------------------------------------
class TestJsonbColumns:
    """VAL-API-064: JSONB columns for unstructured metadata."""

    def test_mission_metadata_is_jsonb(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT data_type, udt_name FROM information_schema.columns "
                    "WHERE table_name='missions' AND column_name='metadata'"
                )
            ).fetchone()
        assert row is not None
        # On PostgreSQL ``udt_name`` is ``jsonb`` for the JSONB type.
        assert row[1] == "jsonb", f"missions.metadata is {row[1]!r}, expected jsonb"

    def test_tasks_config_is_jsonb(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name='tasks' AND column_name='config'"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "jsonb"

    def test_agents_tool_config_is_jsonb(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name='agents' AND column_name='tool_config'"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "jsonb"

    def test_jsonb_supports_nested_data(self, migrated_engine):
        import soy.models as models

        with session_scope(migrated_engine) as session:
            mission = models.Mission(
                title="jsonb test",
                mission_metadata={
                    "labels": ["asf-run", "priority-high"],
                    "config": {"max_retries": 3, "sandbox": True},
                },
            )
            session.add(mission)
            session.flush()
            mid = mission.id
        with session_scope(migrated_engine) as session:
            m = session.get(models.Mission, mid)
            assert m.mission_metadata["labels"] == ["asf-run", "priority-high"]
            assert m.mission_metadata["config"]["max_retries"] == 3


# ---------------------------------------------------------------------------
# Tests — VAL-API-065
# ---------------------------------------------------------------------------
class TestChatSenderType:
    """VAL-API-065: chat_messages.sender_type is an enum with nullable sender_id."""

    def test_sender_type_is_pg_enum(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name='chat_messages' AND column_name='sender_type'"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "chat_sender_type"

    def test_sender_id_is_nullable(self, migrated_engine):
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name='chat_messages' AND column_name='sender_id'"
                )
            ).fetchone()
        assert row is not None
        assert row[0] == "YES", "chat_messages.sender_id must be nullable"

    def test_invalid_sender_type_rejected(self, migrated_engine):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, IntegrityError

        mid = str(uuid.uuid4())
        with migrated_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO missions (id, title, status) "
                        "VALUES (:id, 'x', 'created')"
                    ),
                    {"id": mid},
                )
                with pytest.raises((IntegrityError, DBAPIError)):
                    conn.execute(
                        text(
                            "INSERT INTO chat_messages (id, mission_id, sender_type, content) "
                            "VALUES (gen_random_uuid()::text, :m, 'bot', 'hi')"
                        ),
                        {"m": mid},
                    )

    def test_user_and_system_messages_have_null_sender_id(self, migrated_engine):
        import soy.models as models

        with session_scope(migrated_engine) as session:
            mission = models.Mission(title="chat sender test")
            session.add(mission)
            session.flush()
            user_msg = models.ChatMessage(
                mission_id=mission.id,
                sender_type=models.ChatSenderType.user,
                sender_name="admin",
                content="hello",
            )
            system_msg = models.ChatMessage(
                mission_id=mission.id,
                sender_type=models.ChatSenderType.system,
                content="started",
            )
            session.add_all([user_msg, system_msg])
            session.flush()
            assert user_msg.sender_id is None
            assert system_msg.sender_id is None

    def test_agent_message_links_to_agent(self, migrated_engine):
        import soy.models as models

        with session_scope(migrated_engine) as session:
            mission = models.Mission(title="agent msg test")
            session.add(mission)
            session.flush()
            agent = models.Agent(
                mission_id=mission.id,
                name="coder",
                role=models.AgentRole.coder,
            )
            session.add(agent)
            session.flush()
            agent_msg = models.ChatMessage(
                mission_id=mission.id,
                sender_type=models.ChatSenderType.agent,
                sender_id=agent.id,
                sender_name="coder",
                content="wrote spec.md",
            )
            session.add(agent_msg)
            session.flush()
            mid = mission.id
            aid = agent.id
        with session_scope(migrated_engine) as session:
            msg = (
                session.query(models.ChatMessage)
                .filter(models.ChatMessage.mission_id == mid)
                .one()
            )
            assert msg.sender_id == aid
            assert msg.sender_type == models.ChatSenderType.agent


# ---------------------------------------------------------------------------
# Tests — VAL-CROSS-013
# ---------------------------------------------------------------------------
class TestReupgradeIdempotency:
    """VAL-CROSS-013: re-running upgrade head is a no-op."""

    def test_reupgrade_is_noop(self, db_url):
        from sqlalchemy import create_engine, text

        from soy.db import run_alembic_upgrade

        # First upgrade (idempotent in the schema's lifetime).
        run_alembic_upgrade(database_url=db_url)
        # Second upgrade should be a no-op.
        run_alembic_upgrade(database_url=db_url)

        engine = create_engine(db_url, future=True)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchone()
            assert row is not None
            assert row[0] == "0001_initial_schema"
        finally:
            engine.dispose()

    def test_version_table_tracks_state(self, db_url):
        """The alembic_version table must exist after the first migration."""
        from sqlalchemy import create_engine, text

        from soy.db import run_alembic_upgrade

        run_alembic_upgrade(database_url=db_url)
        engine = create_engine(db_url, future=True)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name='alembic_version'"
                    )
                ).fetchall()
            assert rows, "alembic_version table is missing"
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Module-level setup: ensure the test database is migrated even when the
# ``migrated_engine`` fixture is not used.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _ensure_migrated(db_url):
    """Run ``alembic upgrade head`` once per test module."""
    from soy.db import run_alembic_upgrade

    run_alembic_upgrade(database_url=db_url)
    yield
