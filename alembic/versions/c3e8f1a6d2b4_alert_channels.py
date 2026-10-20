"""job alerts by email as well as Telegram: which channel each watchlist
alert decision is for, and when an emailed alert went out (in a digest).
Existing rows are Telegram alerts.

Revision ID: c3e8f1a6d2b4
Revises: b5e1c7d9f3a2
Create Date: 2026-10-15 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3e8f1a6d2b4'
down_revision: Union[str, None] = 'b5e1c7d9f3a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('watchlist_alerts', sa.Column('channel', sa.String(length=16), nullable=False,
                                                server_default='telegram'))
    op.add_column('watchlist_alerts', sa.Column('sent_at', sa.DateTime(), nullable=True))
    op.create_index('ix_watchlist_alerts_channel_status', 'watchlist_alerts', ['channel', 'status'])


def downgrade() -> None:
    op.drop_index('ix_watchlist_alerts_channel_status', table_name='watchlist_alerts')
    with op.batch_alter_table('watchlist_alerts') as batch:
        batch.drop_column('sent_at')
        batch.drop_column('channel')
