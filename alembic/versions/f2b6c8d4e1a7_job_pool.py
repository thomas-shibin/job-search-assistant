"""job pool: companies' careers-site feeds (Greenhouse, Lever, Ashby) and
their postings with full descriptions, shared by every user; seeded with
companies that hire in Canada (checked 2026-10-14). Postings load on the
first scheduled refresh.

Revision ID: f2b6c8d4e1a7
Revises: e7a1b5c3d2f8
Create Date: 2026-10-14 21:00:00
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2b6c8d4e1a7'
down_revision: Union[str, None] = 'e7a1b5c3d2f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED = [
    # Canadian companies
    ("ashby", "wealthsimple", "Wealthsimple"), ("ashby", "1password", "1Password"), ("ashby", "cohere", "Cohere"),
    ("ashby", "jobber", "Jobber"), ("ashby", "hopper", "Hopper"), ("ashby", "koho", "KOHO"),
    ("ashby", "float", "Float"), ("ashby", "maple", "Maple"), ("greenhouse", "faire", "Faire"),
    ("greenhouse", "geotab", "Geotab"), ("greenhouse", "d2l", "D2L"), ("greenhouse", "ritual", "Ritual"),
    ("greenhouse", "hootsuite", "Hootsuite"), ("greenhouse", "poka", "Poka"),
    # Companies with many Canadian roles
    ("greenhouse", "lyft", "Lyft"), ("greenhouse", "okta", "Okta"), ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "affirm", "Affirm"), ("greenhouse", "stripe", "Stripe"), ("greenhouse", "instacart", "Instacart"),
    ("greenhouse", "brex", "Brex"), ("greenhouse", "elastic", "Elastic"), ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "mongodb", "MongoDB"), ("greenhouse", "asana", "Asana"), ("greenhouse", "pinterest", "Pinterest"),
    ("greenhouse", "reddit", "Reddit"), ("greenhouse", "dropbox", "Dropbox"), ("greenhouse", "airbnb", "Airbnb"),
    ("greenhouse", "coinbase", "Coinbase"), ("greenhouse", "twilio", "Twilio"), ("ashby", "snowflake", "Snowflake"),
    ("ashby", "vanta", "Vanta"), ("ashby", "ramp", "Ramp"), ("ashby", "sanity", "Sanity"),
]


def upgrade() -> None:
    companies = op.create_table(
        "pool_companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("board", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("job_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("canada_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("provider", "board", name="uq_pool_company_provider_board"),
    )
    op.create_table(
        "pool_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("pool_companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("employment_type", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("date_posted", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "external_id", name="uq_pool_job_company_external_id"),
    )
    op.create_index("ix_pool_jobs_company_id", "pool_jobs", ["company_id"])
    op.create_index("ix_pool_jobs_country_posted", "pool_jobs", ["country", "date_posted"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(companies, [{"provider": p, "board": b, "name": n, "enabled": True, "created_at": now,
                                "job_count": 0, "canada_count": 0} for p, b, n in SEED])


def downgrade() -> None:
    op.drop_index("ix_pool_jobs_country_posted", table_name="pool_jobs")
    op.drop_index("ix_pool_jobs_company_id", table_name="pool_jobs")
    op.drop_table("pool_jobs")
    op.drop_table("pool_companies")
