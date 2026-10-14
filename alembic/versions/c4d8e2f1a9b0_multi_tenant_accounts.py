"""multi-tenant: user accounts, sign-in, and an owner on every row of user data

Adds the account tables (users, social identities, sessions, one-time tokens),
per-user settings and search terms, and a `user_id` to every table holding
user data. Jobs become unique per user.

An existing single-user install keeps its data: everything is given to one
owner account -- an admin, with the first address in ADMIN_EMAILS (verified,
so you can sign in with Google/Facebook on that address or reset its
password) or, without ADMIN_EMAILS, a placeholder you claim later with
`python cli.py claim-legacy-data --email you@example.com`. Settings that were
the single user's (search, matching, addresses, ...) move to that owner; the
platform ones (provider, schedule, retention) stay shared. Search terms the
user typed become theirs only.

Revision ID: c4d8e2f1a9b0
Revises: b3c9d1e7f402
Create Date: 2026-10-14 10:00:00
"""
import json
import os
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d8e2f1a9b0'
down_revision: Union[str, None] = 'b3c9d1e7f402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = (
    "ai_call_logs", "application_artifact_sets", "candidate_profiles", "employments", "accomplishment_bullets",
    "skills", "education", "certifications", "projects", "jobs", "job_status_history", "job_requirements",
    "job_tombstones", "match_analyses", "job_fit_assessments", "tailored_resumes", "tailored_bullet_provenance",
    "scheduler_runs", "source_run_results", "watchlist_entries", "watchlist_alerts",
)
USER_SECTIONS = (
    "candidate", "search", "address_profiles", "search_profiles", "matching", "resume", "notifications", "watchlist",
)
LEGACY_PLACEHOLDER_EMAIL = "owner@legacy.invalid"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(), nullable=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("uq_users_account_email", "users", ["email"], unique=True,
                    sqlite_where=sa.text("role != 'guest'"), postgresql_where=sa.text("role != 'guest'"))

    op.create_table(
        "oauth_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),
    )
    op.create_index("ix_oauth_identities_user_id", "oauth_identities", ["user_id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])
    op.create_index("ix_auth_tokens_expires_at", "auth_tokens", ["expires_at"])

    op.create_table(
        "user_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),
    )
    op.create_index("ix_user_settings_user_id", "user_settings", ["user_id"])

    op.create_table(
        "user_search_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("norm", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "kind", "norm", name="uq_user_search_terms_user_kind_norm"),
    )
    op.create_index("ix_user_search_terms_user_id", "user_search_terms", ["user_id"])

    for table in TENANT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_user_id", "users", ["user_id"], ["id"], ondelete="CASCADE")
            batch.create_index(f"ix_{table}_user_id", ["user_id"])

    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("uq_job_source_external_id", type_="unique")
        batch.create_unique_constraint("uq_job_user_source_external_id", ["user_id", "source", "external_id"])

    _assign_legacy_data()


def _has_legacy_data(conn) -> bool:
    for table in ("jobs", "candidate_profiles", "watchlist_entries"):
        if conn.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            return True
    # Or settings someone filled in (a fresh install's seeded defaults are blank).
    app_settings = sa.table("app_settings", sa.column("key"), sa.column("value", sa.JSON))
    values = dict(conn.execute(sa.select(app_settings.c.key, app_settings.c.value)
                               .where(app_settings.c.key.in_(("candidate", "search", "address_profiles")))).all())
    values = {k: json.loads(v) if isinstance(v, str) else v for k, v in values.items()}
    search = values.get("search") or {}
    return bool((values.get("candidate") or {}).get("name", "").strip() or values.get("address_profiles")
                or search.get("titles") or search.get("locations") or search.get("keywords"))


def _assign_legacy_data() -> None:
    conn = op.get_bind()
    users = sa.table("users", *(sa.column(c) for c in (
        "id", "email", "email_verified_at", "first_name", "last_name", "role", "is_active", "failed_logins")))
    owner_id = None
    if _has_legacy_data(conn):
        admins = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
        name = conn.execute(sa.text("SELECT full_name FROM candidate_profiles WHERE full_name IS NOT NULL "
                                    "ORDER BY version DESC LIMIT 1")).scalar() or "Account Owner"
        first, _, last = name.partition(" ")
        conn.execute(sa.insert(users).values(
            email=admins[0] if admins else LEGACY_PLACEHOLDER_EMAIL,
            email_verified_at=_now() if admins else None,
            first_name=first[:100] or "Account", last_name=(last or "Owner")[:100],
            role="admin", is_active=True, failed_logins=0,
        ))
        owner_id = conn.execute(sa.text("SELECT max(id) FROM users")).scalar()
        for table in TENANT_TABLES:
            conn.execute(sa.text(f"UPDATE {table} SET user_id = :owner"), {"owner": owner_id})

    # The single user's settings sections become the owner's; platform ones stay.
    user_settings = sa.table("user_settings", sa.column("user_id"), sa.column("key"), sa.column("value", sa.JSON))
    app_settings = sa.table("app_settings", sa.column("key"), sa.column("value", sa.JSON))
    for key, value in conn.execute(sa.select(app_settings.c.key, app_settings.c.value)
                                   .where(app_settings.c.key.in_(USER_SECTIONS))).all():
        if owner_id is not None:
            conn.execute(sa.insert(user_settings).values(user_id=owner_id, key=key, value=value))
    conn.execute(sa.delete(app_settings).where(app_settings.c.key.in_(USER_SECTIONS)))
    if not conn.execute(sa.select(app_settings.c.key).where(app_settings.c.key == "platform")).first():
        conn.execute(sa.insert(app_settings).values(
            key="platform", value={"job_search_daily_limit": 250, "guest_searches_per_hour": 1}))

    # Search terms the user typed: theirs alone from now on.
    user_terms = sa.table("user_search_terms", *(sa.column(c) for c in ("user_id", "kind", "label", "norm")))
    rows = conn.execute(sa.text("SELECT kind, label, norm FROM search_terms WHERE source = 'user'")).all()
    if owner_id is not None:
        for kind, label, norm in rows:
            conn.execute(sa.insert(user_terms).values(user_id=owner_id, kind=kind, label=label, norm=norm))
    # Keep a typed term in the shared catalog only if the market scan saw it too.
    conn.execute(sa.text("UPDATE search_terms SET source = 'scan' WHERE source = 'user' AND job_count > 0"))
    conn.execute(sa.text("DELETE FROM search_terms WHERE source = 'user'"))


def downgrade() -> None:
    conn = op.get_bind()
    # Settings go back to being global (from the first owner, if any).
    owner = conn.execute(sa.text("SELECT min(user_id) FROM user_settings")).scalar()
    if owner is not None:
        for key, value in conn.execute(sa.text("SELECT key, value FROM user_settings WHERE user_id = :o"),
                                       {"o": owner}).all():
            conn.execute(sa.text("DELETE FROM app_settings WHERE key = :k"), {"k": key})
            conn.execute(sa.table("app_settings", sa.column("key"), sa.column("value", sa.JSON)).insert()
                         .values(key=key, value=value if not isinstance(value, str) else json.loads(value)))
    conn.execute(sa.text("DELETE FROM app_settings WHERE key = 'platform'"))

    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("uq_job_user_source_external_id", type_="unique")
        batch.create_unique_constraint("uq_job_source_external_id", ["source", "external_id"])
    for table in TENANT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_user_id")
            batch.drop_constraint(f"fk_{table}_user_id", type_="foreignkey")
            batch.drop_column("user_id")
    for table in ("user_search_terms", "user_settings", "auth_tokens", "auth_sessions", "oauth_identities"):
        op.drop_table(table)
    op.drop_index("uq_users_account_email", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
