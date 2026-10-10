"""keep the uploaded resume file itself in the database

Revision ID: 5b1e7c0d2f61
Revises: d9ed1ec8c2ca
Create Date: 2026-10-10 10:00:00

Tailoring edits a copy of the original .docx; keeping the file in the database
means it can always start from the exact original design, even if the file on
disk was moved or lost. Existing resumes still on disk are copied in.
"""
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5b1e7c0d2f61'
down_revision: Union[str, None] = 'd9ed1ec8c2ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def upgrade() -> None:
    op.add_column('candidate_profiles', sa.Column('source_file', sa.LargeBinary(), nullable=True))
    connection = op.get_bind()
    profiles = sa.table('candidate_profiles', sa.column('id', sa.Integer), sa.column('raw_resume_path', sa.String),
                        sa.column('source_file', sa.LargeBinary))
    for profile_id, raw_path in connection.execute(sa.select(profiles.c.id, profiles.c.raw_resume_path)).all():
        for path in (Path(raw_path), PROJECT_ROOT / raw_path):
            if path.is_file():
                connection.execute(profiles.update().where(profiles.c.id == profile_id)
                                   .values(source_file=path.read_bytes()))
                break


def downgrade() -> None:
    with op.batch_alter_table('candidate_profiles') as batch:
        batch.drop_column('source_file')
