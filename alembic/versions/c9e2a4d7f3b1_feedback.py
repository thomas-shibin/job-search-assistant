"""feedback: reviews, Contact us messages, and when to ask a member for a review.

Revision ID: c9e2a4d7f3b1
Revises: b4f8c2e6a9d1
Create Date: 2026-10-23 20:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9e2a4d7f3b1'
down_revision: Union[str, None] = 'b4f8c2e6a9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('review_prompt', sa.String(length=16), nullable=True))
    op.add_column('users', sa.Column('review_prompt_after', sa.DateTime(), nullable=True))
    op.create_table(
        'reviews',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('jobs_rating', sa.Integer(), nullable=True),
        sa.Column('enough_jobs_rating', sa.Integer(), nullable=True),
        sa.Column('resume_rating', sa.Integer(), nullable=True),
        sa.Column('features_rating', sa.Integer(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('page', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_reviews_user_id', 'reviews', ['user_id'])
    op.create_index('ix_reviews_created_at', 'reviews', ['created_at'])
    op.create_table(
        'contact_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('topic', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('first_name', sa.String(length=100), nullable=False),
        sa.Column('last_name', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('emailed', sa.Boolean(), nullable=False),
        sa.Column('handled_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_contact_messages_user_id', 'contact_messages', ['user_id'])
    op.create_index('ix_contact_messages_created_at', 'contact_messages', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_contact_messages_created_at', table_name='contact_messages')
    op.drop_index('ix_contact_messages_user_id', table_name='contact_messages')
    op.drop_table('contact_messages')
    op.drop_index('ix_reviews_created_at', table_name='reviews')
    op.drop_index('ix_reviews_user_id', table_name='reviews')
    op.drop_table('reviews')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('review_prompt_after')
        batch.drop_column('review_prompt')
