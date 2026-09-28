"""Migration 0048 adds curie.hook_runs and the slot claim."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _migration_support import IsolatedMigrationDb, alembic_config, sql_dicts
from alembic import command
from curie_api.config import get_settings
from curie_api.models import HookRun
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BELOW = "0047"
REVISION = "0048"
SLOT = datetime(2026, 9, 22, 16, 0, tzinfo=UTC)
STARTED = datetime(2026, 9, 22, 16, 0, 5, tzinfo=UTC)


def _hook_runs_regclass() -> str | None:
    rows = sql_dicts("SELECT to_regclass('curie.hook_runs') AS name")
    name = rows[0]["name"]
    return None if name is None else str(name)


def _seed_agent_version() -> tuple[uuid.UUID, uuid.UUID]:
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    sql_dicts(
        "INSERT INTO curie.agents (id, name) VALUES (:id, :name)",
        {"id": agent_id, "name": f"acme-bot-{agent_id.hex[:8]}"},
    )
    sql_dicts(
        "INSERT INTO curie.agent_versions "
        "(id, agent_id, version_label, bundle_ref, created_by) "
        "VALUES (:id, :agent_id, 'v1', NULL, 'acme')",
        {"id": version_id, "agent_id": agent_id},
    )
    return agent_id, version_id


def _insert_hook_run(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    slot_utc: datetime,
    outcome: str | None,
) -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as session:
                session.add(
                    HookRun(
                        id=uuid.uuid4(),
                        agent_id=agent_id,
                        name="daily-digest",
                        slot_utc=slot_utc,
                        version_id=version_id,
                        outcome=outcome,
                        started_at=STARTED,
                        ended_at=None,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _hook_run_count() -> int:
    rows = sql_dicts("SELECT count(*) AS n FROM curie.hook_runs")
    return int(rows[0]["n"])


def test_0048_creates_hook_runs_and_downgrade_drops_it(
    isolated_migration_db: IsolatedMigrationDb,
) -> None:
    config = alembic_config()
    isolated_migration_db.at(REVISION)
    assert _hook_runs_regclass() is not None
    try:
        command.downgrade(config, BELOW)
        assert _hook_runs_regclass() is None
        command.upgrade(config, REVISION)
        assert _hook_runs_regclass() is not None
    finally:
        # A failed assertion must not leave this private database below head.
        command.upgrade(config, REVISION)


def test_0048_rejects_a_duplicate_slot_and_accepts_another(
    isolated_migration_db: IsolatedMigrationDb,
) -> None:
    isolated_migration_db.at(REVISION)
    agent_id, version_id = _seed_agent_version()
    _insert_hook_run(agent_id, version_id, SLOT, None)
    with pytest.raises(IntegrityError) as excinfo:
        _insert_hook_run(agent_id, version_id, SLOT, None)
    assert "hook_runs_agent_name_slot_key" in str(excinfo.value)
    assert _hook_run_count() == 1
    _insert_hook_run(agent_id, version_id, SLOT + timedelta(days=1), None)
    assert _hook_run_count() == 2


def test_0048_rejects_deferred_outcome(isolated_migration_db: IsolatedMigrationDb) -> None:
    isolated_migration_db.at(REVISION)
    agent_id, version_id = _seed_agent_version()
    with pytest.raises(IntegrityError) as excinfo:
        _insert_hook_run(agent_id, version_id, SLOT, "deferred")
    assert "hook_runs_outcome_ck" in str(excinfo.value)
