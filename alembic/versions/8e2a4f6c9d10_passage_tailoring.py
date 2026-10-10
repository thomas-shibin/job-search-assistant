"""passage tailoring: keep each rewritten paragraph's original text and section

Revision ID: 8e2a4f6c9d10
Revises: 5b1e7c0d2f61
Create Date: 2026-10-10 12:00:00

Word resumes are tailored paragraph by paragraph, whatever their layout; a
rewritten paragraph may have no parsed source bullet, so its original text
and heading are stored with it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8e2a4f6c9d10'
down_revision: Union[str, None] = '5b1e7c0d2f61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tailored_bullet_provenance', sa.Column('source_text', sa.Text(), nullable=True))
    op.add_column('tailored_bullet_provenance', sa.Column('section', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tailored_bullet_provenance') as batch:
        batch.drop_column('section')
        batch.drop_column('source_text')
