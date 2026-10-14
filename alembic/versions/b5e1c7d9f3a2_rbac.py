"""role-based access control: roles, permissions, user roles, role
permissions, feature switches and the audit log. Seeds the built-in roles and
permissions (a frozen copy of app/auth/rbac.py as of this revision; later
additions are synced at startup) and gives existing accounts their roles:
admins become SUPER_ADMIN (and keep JOB_SEEKER), members become JOB_SEEKER.
Repeatable: rows that already exist are left alone.

Revision ID: b5e1c7d9f3a2
Revises: a9d3e5f7b2c1
Create Date: 2026-10-14 16:00:00
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b5e1c7d9f3a2'
down_revision: Union[str, None] = 'a9d3e5f7b2c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS = [   ('jobs', 'view', 'Search jobs and view job details'),
    ('jobs', 'save', 'Save jobs to come back to'),
    ('jobs', 'ai.match', 'Get a match score and gap analysis for a job'),
    ('jobs', 'create', 'Post a job (employers)'),
    ('jobs', 'update.own', 'Edit job postings of their own company'),
    ('jobs', 'update.any', 'Edit any job posting'),
    ('jobs', 'delete.own', 'Delete job postings of their own company'),
    ('jobs', 'delete.any', 'Delete any job posting'),
    ('applications', 'create', 'Track a job they are applying to'),
    ('applications', 'view.own', 'See their own applications'),
    ('applications', 'update.own', 'Update the status of their own applications'),
    ('applications', 'view.company', "See applicants to their company's jobs"),
    ('applications', 'view.any', 'See every application on the platform'),
    ('companies', 'view', 'View company profiles'),
    ('companies', 'create', 'Create a company profile'),
    ('companies', 'update.own', "Edit their own company's profile"),
    ('companies', 'update.any', 'Edit any company profile'),
    ('profiles', 'view.own', 'View their own profile and settings'),
    ('profiles', 'update.own', 'Edit their own profile and settings'),
    ('resumes', 'upload', 'Upload and edit their resume'),
    ('resumes', 'view.own', 'View and download their own resume'),
    ('resumes', 'ai.generate', 'Create tailored resumes (Quick Tailor, per-job tailoring)'),
    ('alerts', 'manage', 'Set up job alerts and Telegram notifications'),
    ('users', 'view', 'View user accounts and their activity'),
    ('users', 'create', 'Create user accounts'),
    ('users', 'update', "Change a user's hourly limits"),
    ('users', 'suspend', 'Suspend and reactivate accounts'),
    ('users', 'roles.assign', "Assign and remove users' roles"),
    ('roles', 'view', 'View roles and their permissions'),
    ('roles', 'create', 'Create custom roles'),
    ('roles', 'update', 'Edit roles'),
    ('roles', 'delete', 'Delete unused custom roles'),
    ('permissions', 'view', 'View the permission catalog'),
    ('permissions', 'manage', 'Change which permissions a role has'),
    ('analytics', 'view.company', "View their company's recruiting analytics"),
    ('analytics', 'view.platform', 'View platform activity, usage and costs'),
    ('features', 'view', 'View feature switches'),
    ('features', 'manage', 'Turn optional features on or off'),
    ('settings', 'view', 'View platform settings and health'),
    ('settings', 'manage', 'Change platform settings and run scans'),
    ('sources', 'view', 'View the company job sources'),
    ('sources', 'manage', 'Add, refresh and remove company job sources'),
    ('audit_logs', 'view', 'View the audit log'),
    ('admin.dashboard', 'view', 'Open the admin dashboard'),
    ('employer.dashboard', 'view', 'Open the employer workspace')]

SYSTEM_ROLES = {   'SUPER_ADMIN': ("Full access to everything, including roles and permissions. Can't be restricted.", []),
    'ADMIN': (   'Runs the platform: users, job sources, features and analytics.',
                 [   'admin.dashboard.view',
                     'users.view',
                     'users.update',
                     'users.suspend',
                     'users.roles.assign',
                     'roles.view',
                     'permissions.view',
                     'analytics.view.platform',
                     'features.view',
                     'features.manage',
                     'settings.view',
                     'audit_logs.view',
                     'sources.view',
                     'sources.manage',
                     'jobs.view',
                     'jobs.update.any',
                     'jobs.delete.any',
                     'companies.view',
                     'companies.update.any',
                     'applications.view.any']),
    'EMPLOYER': (   "Manages their company's profile, job postings and applicants.",
                    [   'jobs.view',
                        'jobs.create',
                        'jobs.update.own',
                        'jobs.delete.own',
                        'applications.view.company',
                        'companies.view',
                        'companies.create',
                        'companies.update.own',
                        'analytics.view.company',
                        'employer.dashboard.view',
                        'profiles.view.own',
                        'profiles.update.own']),
    'JOB_SEEKER': (   'Searches and saves jobs, tracks applications, manages their profile and resume.',
                      [   'jobs.view',
                          'jobs.save',
                          'jobs.ai.match',
                          'applications.create',
                          'applications.view.own',
                          'applications.update.own',
                          'profiles.view.own',
                          'profiles.update.own',
                          'resumes.upload',
                          'resumes.view.own',
                          'resumes.ai.generate',
                          'alerts.manage'])}

FEATURES = {   'job_search': ('Job search', 'Searching jobs and viewing postings.', ['jobs.view']),
    'saved_jobs': ('Saved jobs', 'Saving jobs to come back to later.', ['jobs.save']),
    'application_tracking': (   'Application tracking',
                                'Tracking jobs through applied, interview and offer.',
                                ['applications.view.own']),
    'job_alerts': ('Job alerts', 'Scheduled searches with alerts by Telegram.', ['alerts.manage']),
    'resume_builder': (   'Resume editor',
                          'Editing the resume section by section in the browser.',
                          ['resumes.upload']),
    'ai_resume_builder': (   'Resume tailoring',
                             'Quick Tailor and tailored resumes for each job.',
                             ['resumes.ai.generate']),
    'ai_job_matching': (   'Job matching',
                           "Match scores and gap analysis against the user's resume.",
                           ['jobs.ai.match'])}


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False, unique=True),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("is_system_role", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("resource", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", sa.Integer(), sa.ForeignKey("permissions.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature_key", sa.String(length=60), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("required_permissions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_logs_created", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    _seed(op.get_bind())


def _seed(bind) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    roles_t = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String),
                       sa.column("description", sa.String), sa.column("is_system_role", sa.Boolean),
                       sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime))
    perms_t = sa.table("permissions", sa.column("id", sa.Integer), sa.column("name", sa.String),
                       sa.column("description", sa.String), sa.column("resource", sa.String),
                       sa.column("action", sa.String), sa.column("created_at", sa.DateTime))
    grants_t = sa.table("role_permissions", sa.column("role_id", sa.Integer), sa.column("permission_id", sa.Integer))
    flags_t = sa.table("feature_flags", sa.column("feature_key", sa.String), sa.column("name", sa.String),
                       sa.column("description", sa.String), sa.column("enabled", sa.Boolean),
                       sa.column("required_permissions", sa.JSON), sa.column("created_at", sa.DateTime),
                       sa.column("updated_at", sa.DateTime))
    user_roles_t = sa.table("user_roles", sa.column("user_id", sa.Integer), sa.column("role_id", sa.Integer),
                            sa.column("created_at", sa.DateTime))

    role_ids = dict(bind.execute(sa.select(roles_t.c.name, roles_t.c.id)).all())
    for name, (description, _) in SYSTEM_ROLES.items():
        if name not in role_ids:
            bind.execute(roles_t.insert().values(name=name, description=description, is_system_role=True,
                                                 created_at=now, updated_at=now))
    role_ids = dict(bind.execute(sa.select(roles_t.c.name, roles_t.c.id)).all())

    perm_ids = dict(bind.execute(sa.select(perms_t.c.name, perms_t.c.id)).all())
    for resource, action, description in PERMISSIONS:
        if f"{resource}.{action}" not in perm_ids:
            bind.execute(perms_t.insert().values(name=f"{resource}.{action}", description=description,
                                                 resource=resource, action=action, created_at=now))
    perm_ids = dict(bind.execute(sa.select(perms_t.c.name, perms_t.c.id)).all())

    granted = set(bind.execute(sa.select(grants_t.c.role_id, grants_t.c.permission_id)).all())
    for name, (_, defaults) in SYSTEM_ROLES.items():
        for perm in defaults:
            pair = (role_ids[name], perm_ids[perm])
            if pair not in granted:
                bind.execute(grants_t.insert().values(role_id=pair[0], permission_id=pair[1]))
                granted.add(pair)

    flags = set(bind.execute(sa.select(flags_t.c.feature_key)).scalars())
    for key, (name, description, required) in FEATURES.items():
        if key not in flags:
            bind.execute(flags_t.insert().values(feature_key=key, name=name, description=description, enabled=True,
                                                 required_permissions=required, created_at=now, updated_at=now))

    # Existing accounts: admins -> SUPER_ADMIN + JOB_SEEKER, members -> JOB_SEEKER (guests get nothing).
    held = set(bind.execute(sa.select(user_roles_t.c.user_id, user_roles_t.c.role_id)).all())
    for user_id, legacy in bind.execute(sa.text("SELECT id, role FROM users")).all():
        wanted = {"admin": ["SUPER_ADMIN", "JOB_SEEKER"], "member": ["JOB_SEEKER"]}.get(legacy, [])
        for name in wanted:
            if (user_id, role_ids[name]) not in held:
                bind.execute(user_roles_t.insert().values(user_id=user_id, role_id=role_ids[name], created_at=now))


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("feature_flags")
    op.drop_table("role_permissions")
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
