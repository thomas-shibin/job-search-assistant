"""onboarding: email codes (sign-up / sign-in by code), account type,
job-seeker profiles with experience and education, saved jobs, and
workplace / salary on job-pool postings

Revision ID: a9d3e5f7b2c1
Revises: f2b6c8d4e1a7
Create Date: 2026-10-15 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a9d3e5f7b2c1'
down_revision: Union[str, None] = 'f2b6c8d4e1a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _owner():
    return sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("account_type", sa.String(length=16), nullable=False, server_default="job_seeker"))

    op.create_table(
        "email_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_email_codes_email_created", "email_codes", ["email", "created_at"])

    op.create_table(
        "job_seeker_profiles",
        sa.Column("id", sa.Integer(), primary_key=True), _owner(),
        sa.Column("city", sa.String(length=120)), sa.Column("province", sa.String(length=120)),
        sa.Column("country", sa.String(length=120)), sa.Column("headline", sa.String(length=200)),
        sa.Column("current_title", sa.String(length=200)), sa.Column("years_experience", sa.Integer()),
        sa.Column("skills", sa.JSON(), nullable=False), sa.Column("preferred_titles", sa.JSON(), nullable=False),
        sa.Column("preferred_locations", sa.JSON(), nullable=False),
        sa.Column("employment_types", sa.JSON(), nullable=False), sa.Column("workplaces", sa.JSON(), nullable=False),
        sa.Column("prompt_dismissed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_job_seeker_profile_user"),
    )
    op.create_table(
        "profile_experiences",
        sa.Column("id", sa.Integer(), primary_key=True), _owner(),
        sa.Column("job_title", sa.String(length=200), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=False), sa.Column("location", sa.String(length=200)),
        sa.Column("start_date", sa.Date()), sa.Column("end_date", sa.Date()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text()), sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_profile_experiences_user_id", "profile_experiences", ["user_id"])
    op.create_table(
        "profile_educations",
        sa.Column("id", sa.Integer(), primary_key=True), _owner(),
        sa.Column("institution", sa.String(length=200), nullable=False),
        sa.Column("qualification", sa.String(length=200)), sa.Column("field_of_study", sa.String(length=200)),
        sa.Column("start_date", sa.Date()), sa.Column("end_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_profile_educations_user_id", "profile_educations", ["user_id"])
    op.create_table(
        "saved_jobs",
        sa.Column("id", sa.Integer(), primary_key=True), _owner(),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "job_id", name="uq_saved_job_user_job"),
    )
    op.create_index("ix_saved_jobs_user_id", "saved_jobs", ["user_id"])
    op.create_index("ix_saved_jobs_job_id", "saved_jobs", ["job_id"])

    with op.batch_alter_table("pool_jobs") as batch:
        batch.add_column(sa.Column("workplace", sa.String(length=8)))
        batch.add_column(sa.Column("salary_min", sa.Integer()))
        batch.add_column(sa.Column("salary_max", sa.Integer()))
        batch.add_column(sa.Column("currency", sa.String(length=8)))
    # Postings pick up workplace / salary on their next refresh.
    op.execute("UPDATE pool_companies SET last_synced_at = NULL")


def downgrade() -> None:
    with op.batch_alter_table("pool_jobs") as batch:
        for column in ("currency", "salary_max", "salary_min", "workplace"):
            batch.drop_column(column)
    op.drop_table("saved_jobs")
    op.drop_table("profile_educations")
    op.drop_table("profile_experiences")
    op.drop_table("job_seeker_profiles")
    op.drop_index("ix_email_codes_email_created", table_name="email_codes")
    op.drop_table("email_codes")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("account_type")
