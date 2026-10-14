"""activity log and hourly limits: what each user did (for the admin activity
dashboard and the hourly search / resume-action limits), plus per-user limit
overrides set by an admin

Revision ID: e7a1b5c3d2f8
Revises: c4d8e2f1a9b0
Create Date: 2026-10-14 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e7a1b5c3d2f8'
down_revision: Union[str, None] = 'c4d8e2f1a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=True),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_activity_events_user_id", "activity_events", ["user_id"])
    op.create_index("ix_activity_events_created_at", "activity_events", ["created_at"])
    op.create_index("ix_activity_events_user_kind_time", "activity_events", ["user_id", "kind", "created_at"])
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("search_limit_per_hour", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("ai_limit_per_hour", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("ai_limit_per_hour")
        batch.drop_column("search_limit_per_hour")
    op.drop_index("ix_activity_events_user_kind_time", table_name="activity_events")
    op.drop_index("ix_activity_events_created_at", table_name="activity_events")
    op.drop_index("ix_activity_events_user_id", table_name="activity_events")
    op.drop_table("activity_events")
