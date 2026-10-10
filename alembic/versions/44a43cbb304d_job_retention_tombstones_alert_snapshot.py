"""job retention: tombstones, alert snapshot

Revision ID: 44a43cbb304d
Revises: baac7e948349
Create Date: 2026-10-09 18:32:27.169824

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '44a43cbb304d'
down_revision: Union[str, None] = 'baac7e948349'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _alerts_table(name: str, job_fk_ondelete: str, job_id_nullable: bool, with_snapshot: bool):
    columns = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=job_id_nullable),
    ]
    if with_snapshot:
        columns += [sa.Column("job_title", sa.String(length=255), nullable=True),
                    sa.Column("job_company", sa.String(length=255), nullable=True)]
    columns += [
        sa.Column("alert_key", sa.String(length=512), nullable=False),
        sa.Column("watches", sa.JSON(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete=job_fk_ondelete),
        sa.PrimaryKeyConstraint("id"),
    ]
    op.create_table(name, *columns)


def _rebuild_alerts(job_fk_ondelete: str, job_id_nullable: bool, with_snapshot: bool) -> None:
    """watchlist_alerts' foreign key changes, and SQLite can't alter a constraint
    in place: build the new table, copy the rows over, swap it in."""
    for index in ("ix_watchlist_alerts_alert_key", "ix_watchlist_alerts_created_at", "ix_watchlist_alerts_job_id"):
        op.drop_index(index, table_name="watchlist_alerts")
    _alerts_table("watchlist_alerts_new", job_fk_ondelete, job_id_nullable, with_snapshot)
    shared = "id, job_id, alert_key, watches, score, status, detail, created_at"
    if with_snapshot:
        op.execute(
            f"INSERT INTO watchlist_alerts_new ({shared}, job_title, job_company) "
            f"SELECT a.id, a.job_id, a.alert_key, a.watches, a.score, a.status, a.detail, a.created_at, j.title, j.company "
            f"FROM watchlist_alerts a LEFT JOIN jobs j ON j.id = a.job_id"
        )
    else:
        op.execute(f"INSERT INTO watchlist_alerts_new ({shared}) SELECT {shared} FROM watchlist_alerts WHERE job_id IS NOT NULL")
    op.drop_table("watchlist_alerts")
    op.rename_table("watchlist_alerts_new", "watchlist_alerts")
    op.create_index("ix_watchlist_alerts_alert_key", "watchlist_alerts", ["alert_key"])
    op.create_index("ix_watchlist_alerts_created_at", "watchlist_alerts", ["created_at"])
    op.create_index("ix_watchlist_alerts_job_id", "watchlist_alerts", ["job_id"])


def upgrade() -> None:
    op.create_table(
        "job_tombstones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("description_hash", sa.String(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_tombstones_deleted_at", "job_tombstones", ["deleted_at"])
    op.create_index("ix_job_tombstones_description_hash", "job_tombstones", ["description_hash"])
    op.create_index("ix_job_tombstones_external_id", "job_tombstones", ["external_id"])
    op.create_index("ix_job_tombstones_fingerprint", "job_tombstones", ["fingerprint"])
    _rebuild_alerts("SET NULL", job_id_nullable=True, with_snapshot=True)


def downgrade() -> None:
    _rebuild_alerts("CASCADE", job_id_nullable=False, with_snapshot=False)
    for index in ("ix_job_tombstones_fingerprint", "ix_job_tombstones_external_id",
                  "ix_job_tombstones_description_hash", "ix_job_tombstones_deleted_at"):
        op.drop_index(index, table_name="job_tombstones")
    op.drop_table("job_tombstones")
