"""the free plan's second allowance: job-board searches in total per person.

Revision ID: b4f8c2e6a9d1
Revises: a7d3e9f1c5b2
Create Date: 2026-10-23 14:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4f8c2e6a9d1'
down_revision: Union[str, None] = 'a7d3e9f1c5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('search_total_limit', sa.Integer(), nullable=True))
    op.add_column('free_plan_accounts', sa.Column('searches_used', sa.Integer(), nullable=False,
                                                  server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('free_plan_accounts') as batch:
        batch.drop_column('searches_used')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('search_total_limit')
