"""Exercise timezone conversion against the actual pre-upgrade SQLite schema."""

# Legacy database values are deliberately naive.

import importlib
from datetime import UTC, datetime
from io import StringIO
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from open_ire.db import get_alembic_config

PREVIOUS_REVISION = "473c9757ac3f"
REVISION = "9843ce2b584f"
migration = importlib.import_module(f"open_ire.migrations.versions.{REVISION}_aware_datetime")


@pytest.fixture
def legacy_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = get_alembic_config(url)
    command.upgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(url)
    metadata = sa.MetaData()
    metadata.reflect(engine)
    timestamps = [
        datetime(2026, 1, 15, 12, 30, 45, 123456),
        datetime(2026, 7, 15, 12, 30, 45, 654321),
    ]
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        for index, timestamp in enumerate(timestamps, start=1):
            article_id = uuid4().hex
            records = {
                "article": {
                    "id": article_id,
                    "reference": str(index),
                    "repository": "test",
                    "title": "Test article",
                    "url": f"https://example.com/{index}",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
                "author": {
                    "id": index,
                    "full_name": "Test Author",
                    "canonical_name": "Author, Test",
                    "explicitly_searched": False,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
                "article_file": {
                    "id": uuid4().hex,
                    "article_id": article_id,
                    "url": f"https://example.com/{index}.pdf",
                    "checksum": "test",
                    "path": "test.pdf",
                    "created_at": timestamp,
                },
                "article_file_reference": {
                    "id": uuid4().hex,
                    "article_id": article_id,
                    "url": f"https://example.com/reference/{index}",
                    "created_at": timestamp,
                },
                "article_oa_evidence": {
                    "id": uuid4().hex,
                    "article_id": article_id,
                    "kind": "LICENSE",
                    "supports_oa": True,
                    "created_at": timestamp,
                },
                "article_deposit_status_transition": {
                    "id": uuid4().hex,
                    "article_id": article_id,
                    "to_status": "READY",
                    "changed_at": timestamp,
                },
                "author_identifier": {
                    "id": index,
                    "author_id": index,
                    "authority": "test",
                    "identifier": str(index),
                    "created_at": timestamp,
                },
                "authorship": {
                    "article_id": article_id,
                    "author_id": index,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            }
            for name, values in records.items():
                connection.execute(metadata.tables[name].insert().values(**values))
    yield config, engine, metadata
    engine.dispose()


def snapshot(engine, metadata):
    with engine.connect() as connection:
        return {
            table.name: connection.execute(sa.select(table).order_by(*table.primary_key.columns))
            .mappings()
            .all()
            for table in metadata.sorted_tables
        }


def test_upgrade_and_downgrade_preserve_instants_and_relationships(legacy_database):
    config, engine, metadata = legacy_database
    before = snapshot(engine, metadata)
    command.upgrade(config, REVISION)
    after = snapshot(engine, metadata)
    timestamp_count = 0
    for table_name, rows in before.items():
        if table_name == "alembic_version":
            continue
        for original, converted in zip(rows, after[table_name], strict=True):
            for name, value in original.items():
                if isinstance(value, datetime):
                    # Noon in Pacific time is 20:00 UTC in winter, 19:00 in summer.
                    assert converted[name] == value.replace(hour=20 if value.month == 1 else 19)
                    timestamp_count += 1
                else:
                    assert converted[name] == value
    assert timestamp_count == 22
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    command.upgrade(config, REVISION)
    assert snapshot(engine, metadata) == after
    command.downgrade(config, PREVIOUS_REVISION)
    assert snapshot(engine, metadata) == before


@pytest.mark.parametrize("timestamp", [datetime(2026, 3, 8, 2, 30), datetime(2026, 11, 1, 1, 30)])
def test_unresolvable_dst_timestamp_rolls_back_all_tables(legacy_database, timestamp):
    config, engine, metadata = legacy_database
    with engine.begin() as connection:
        connection.execute(metadata.tables["authorship"].update().values(updated_at=timestamp))
    before = snapshot(engine, metadata)
    with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
        command.upgrade(config, REVISION)
    assert snapshot(engine, metadata) == before


def test_new_utc_values_downgrade_to_local_time(legacy_database):
    config, engine, metadata = legacy_database
    command.upgrade(config, REVISION)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["author"]
            .update()
            .values(updated_at=datetime(2026, 7, 15, 22, tzinfo=UTC))
        )
    command.downgrade(config, PREVIOUS_REVISION)
    with engine.connect() as connection:
        timestamps = connection.execute(sa.select(metadata.tables["author"].c.updated_at)).scalars()
        assert list(timestamps) == [datetime(2026, 7, 15, 15)] * 2


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_sql_explicitly_uses_source_timezone(direction):
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        getattr(migration, direction)()
    sql = output.getvalue()
    timezone_type = "WITH" if direction == "upgrade" else "WITHOUT"
    assert sql.count(f"TYPE TIMESTAMP {timezone_type} TIME ZONE") == 11
    assert sql.count("AT TIME ZONE 'America/Los_Angeles'") == 11


def test_fresh_database_can_upgrade(tmp_path):
    config = get_alembic_config(f"sqlite:///{tmp_path / 'fresh.db'}")
    command.upgrade(config, REVISION)
