"""help: the questions asked in the help chat and Help centre search (who asked isn't kept).

Revision ID: d5b1f8e3a7c2
Revises: c9e2a4d7f3b1
Create Date: 2026-10-24 09:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5b1f8e3a7c2'
down_revision: Union[str, None] = 'c9e2a4d7f3b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'help_questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('question', sa.String(length=300), nullable=False),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('answered', sa.Boolean(), nullable=False),
        sa.Column('article', sa.String(length=80), nullable=True),
        sa.Column('section', sa.String(length=200), nullable=True),
        sa.Column('page', sa.String(length=200), nullable=True),
        sa.Column('signed_in', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_help_questions_created_at', 'help_questions', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_help_questions_created_at', table_name='help_questions')
    op.drop_table('help_questions')
