"""suggested removals: the AI may flag a paragraph as irrelevant to the job;
the user decides whether it's removed

Revision ID: b3c9d1e7f402
Revises: 8e2a4f6c9d10
Create Date: 2026-10-10 14:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b3c9d1e7f402'
down_revision: Union[str, None] = '8e2a4f6c9d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tailored_bullet_provenance',
                  sa.Column('suggest_remove', sa.Boolean(), server_default='0', nullable=False))
    op.add_column('tailored_bullet_provenance', sa.Column('remove_reason', sa.Text(), nullable=True))
    op.add_column('tailored_bullet_provenance', sa.Column('removed', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tailored_bullet_provenance') as batch:
        batch.drop_column('removed')
        batch.drop_column('remove_reason')
        batch.drop_column('suggest_remove')
