"""the free plan: a lifetime allowance of resume actions per person, and the
hashed marks that find one person's accounts (app/modules/usage/free_plan.py).

Revision ID: a7d3e9f1c5b2
Revises: c3e8f1a6d2b4
Create Date: 2026-10-23 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7d3e9f1c5b2'
down_revision: Union[str, None] = 'c3e8f1a6d2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('ai_total_limit', sa.Integer(), nullable=True))
    op.create_table(
        'free_plan_accounts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=True, unique=True),
        sa.Column('ai_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'free_plan_marks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('free_plan_accounts.id'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('account_id', 'kind', 'digest', name='uq_free_plan_marks_account_kind_digest'),
    )
    op.create_index('ix_free_plan_marks_account_id', 'free_plan_marks', ['account_id'])
    op.create_index('ix_free_plan_marks_kind_digest', 'free_plan_marks', ['kind', 'digest'])
    op.create_table(
        'free_plan_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_a', sa.Integer(), sa.ForeignKey('free_plan_accounts.id'), nullable=False),
        sa.Column('account_b', sa.Integer(), sa.ForeignKey('free_plan_accounts.id'), nullable=False),
        sa.Column('reasons', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.UniqueConstraint('account_a', 'account_b', name='uq_free_plan_links_pair'),
    )
    op.create_index('ix_free_plan_links_account_a', 'free_plan_links', ['account_a'])
    op.create_index('ix_free_plan_links_account_b', 'free_plan_links', ['account_b'])


def downgrade() -> None:
    op.drop_table('free_plan_links')
    op.drop_table('free_plan_marks')
    op.drop_table('free_plan_accounts')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('ai_total_limit')
