"""Use timezone-aware datetimes (default for SQLModel 0.0.45)

Revision ID: 9843ce2b584f
Revises: 473c9757ac3f
Create Date: 2026-09-21 21:01:08.459535

Existing timestamps are America/Los_Angeles wall times. Run this revision
before writing UTC timestamps; SQLite cannot distinguish the two conventions.
SQLite keeps its DATETIME columns and stores UTC without an offset, which
SQLModel 0.0.45 reads as aware UTC. PostgreSQL changes the column types too.

Ambiguous or nonexistent local times at DST transitions must be resolved
before upgrading. Downgrading loses timezone information again.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "9843ce2b584f"
down_revision: str | None = "473c9757ac3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TIMEZONE = "America/Los_Angeles"
_TIMESTAMP_COLUMNS = {
    "article": ("created_at", "updated_at"),
    "article_file": ("created_at",),
    "article_file_reference": ("created_at",),
    "article_oa_evidence": ("created_at",),
    "article_deposit_status_transition": ("changed_at",),
    "author": ("created_at", "updated_at"),
    "author_identifier": ("created_at",),
    "authorship": ("created_at", "updated_at"),
}


def _convert_datetime(value: datetime | None, *, to_utc: bool) -> datetime | None:
    if value is None:
        return None

    local_zone = ZoneInfo(_SOURCE_TIMEZONE)
    if to_utc:
        if value.tzinfo is None:
            first = value.replace(tzinfo=local_zone, fold=0)
            second = value.replace(tzinfo=local_zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                msg = (
                    f"Ambiguous or nonexistent local timestamp {value} in "
                    f"{_SOURCE_TIMEZONE}; resolve it before migrating."
                )
                raise ValueError(msg)
            value = first
        return value.astimezone(UTC).replace(tzinfo=None)

    return value.replace(tzinfo=UTC).astimezone(local_zone).replace(tzinfo=None)


def _convert_sqlite(*, to_utc: bool) -> None:
    if context.is_offline_mode():
        msg = "The SQLite datetime data migration requires an online connection."
        raise RuntimeError(msg)

    connection = op.get_bind()
    # Use historical SQLAlchemy types, not current SQLModel models, so reads
    # retain the naive values that need conversion. A savepoint rolls back all
    # tables if any timestamp cannot be converted.
    with connection.begin_nested():
        for table_name, column_names in _TIMESTAMP_COLUMNS.items():
            table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
            primary_keys = list(table.primary_key.columns)
            columns = [table.c[name] for name in column_names]
            statement = (
                table.update()
                .where(
                    sa.and_(
                        *(column == sa.bindparam(f"pk_{column.name}") for column in primary_keys)
                    )
                )
                .values({name: sa.bindparam(f"new_{name}") for name in column_names})
            )
            with connection.execute(sa.select(*primary_keys, *columns)) as result:
                for rows in result.mappings().partitions(500):
                    updates = []
                    for row in rows:
                        values = {f"pk_{column.name}": row[column.name] for column in primary_keys}
                        values.update(
                            {
                                f"new_{name}": _convert_datetime(row[name], to_utc=to_utc)
                                for name in column_names
                            }
                        )
                        updates.append(values)
                    connection.execute(statement, updates)


def _migrate(*, to_utc: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _convert_sqlite(to_utc=to_utc)
    elif dialect == "postgresql":
        for table_name, column_names in _TIMESTAMP_COLUMNS.items():
            for column_name in column_names:
                op.alter_column(
                    table_name,
                    column_name,
                    existing_type=sa.DateTime(timezone=not to_utc),
                    type_=sa.DateTime(timezone=to_utc),
                    postgresql_using=f"{column_name} AT TIME ZONE '{_SOURCE_TIMEZONE}'",
                )
    else:
        msg = f"Unsupported dialect for datetime migration: {dialect}"
        raise NotImplementedError(msg)


def upgrade() -> None:
    _migrate(to_utc=True)


def downgrade() -> None:
    _migrate(to_utc=False)
