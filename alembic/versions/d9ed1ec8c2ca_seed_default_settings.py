"""seed default settings

Revision ID: d9ed1ec8c2ca
Revises: 44a43cbb304d
Create Date: 2026-10-09 18:34:47.876396

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9ed1ec8c2ca'
down_revision: Union[str, None] = '44a43cbb304d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen snapshot of the default settings as of this release (a later change
# to a default ships as a new revision, never an edit here). User-specific
# values start blank: name, resume, job titles, locations, keywords, addresses,
# company boards, watches. Secrets live in .env, never here.
DEFAULTS = {
    "candidate": {"name": "", "base_resume": "", "override_parsed_name": False},
    "search": {
        "titles": [], "locations": [], "keywords": [], "country": "ca",
        "scan_in_background": True, "adzuna_daily_limit": 250,
    },
    "address_profiles": {},
    "search_profiles": [],
    "matching": {
        "minimum_score": 75,
        "required_or_high_value_skills": [], "preferred_skills": [], "negative_keywords": [],
        "weights": {
            "technical_skills": 30, "relevant_experience": 20, "title_similarity": 15, "seniority": 10,
            "platform": 10, "location": 5, "domain_experience": 5, "education": 5,
        },
    },
    "ai": {"enabled": False, "provider": "openai", "model": "gpt-4o-mini", "generate_match_explanations": False},
    "resume": {
        "optimize_keywords": True, "reorder_bullets": True, "tailor_summary": True, "tailor_skills": True,
        "change_address": True, "maximum_pages": 2, "never_invent_experience": True,
    },
    "notifications": {"minimum_score": 80},
    "watchlist": {"enabled": False, "max_alerts_per_run": 15, "dashboard_url": None, "telegram_chat_id": None},
    "scheduler": {"enabled": True, "interval_minutes": 60},
    "retention": {"delete_jobs_after_days": 7, "keep_jobs_you_acted_on": True},
}


def upgrade() -> None:
    from app.core.config.seeding import seed_setting_defaults

    seed_setting_defaults(op.get_bind(), DEFAULTS)


def downgrade() -> None:
    # Settings are user data once seeded; leave them in place.
    pass
