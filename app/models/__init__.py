"""Every database model, imported in one place so SQLAlchemy's metadata (and
Alembic, see alembic/env.py) sees the whole schema. Each model lives in the
module that owns it; import it from there in application code."""
from app.core.config.models import AppSetting, UserSetting, WatchlistEntry
from app.modules.access.models import AuditLog, FeatureFlag, Permission, RbacRole, RolePermission, UserRole
from app.modules.alerts.models import WatchlistAlert
from app.modules.applications.models import ApplicationArtifactSet
from app.modules.auth.models import AuthSession, AuthToken, EmailCode, OAuthIdentity, Role, TokenPurpose, User
from app.modules.feedback.models import ContactMessage, Review
from app.modules.help.models import HelpQuestion
from app.modules.jobs.catalog.models import LocationType, SearchTerm, TermKind, TermSource, UserSearchTerm
from app.modules.jobs.models import Job, JobRequirements, JobStatus, JobStatusHistory, JobTombstone, SavedJob
from app.modules.jobs.pool.models import PoolCompany, PoolJob
from app.modules.matching.models import JobFitAssessment, MatchAnalysis
from app.modules.profiles.models import JobSeekerProfile, ProfileEducation, ProfileExperience
from app.modules.resumes.models import (
    AccomplishmentBullet,
    CandidateProfile,
    Certification,
    Education,
    Employment,
    Project,
    Skill,
)
from app.modules.resumes.tailoring_models import TailoredBulletProvenance, TailoredResume
from app.modules.usage.ai_calls import AICallLog
from app.modules.usage.free_plan_models import AccountLink, FreeAccount, PersonMark
from app.modules.usage.models import ActivityEvent, ActivityKind
from app.workers.models import SchedulerRun, SourceRunResult

__all__ = [
    "AppSetting", "UserSetting", "WatchlistEntry",
    "AuditLog", "FeatureFlag", "Permission", "RbacRole", "RolePermission", "UserRole",
    "WatchlistAlert",
    "ApplicationArtifactSet",
    "AuthSession", "AuthToken", "EmailCode", "OAuthIdentity", "Role", "TokenPurpose", "User",
    "ContactMessage", "Review",
    "HelpQuestion",
    "LocationType", "SearchTerm", "TermKind", "TermSource", "UserSearchTerm",
    "Job", "JobRequirements", "JobStatus", "JobStatusHistory", "JobTombstone", "SavedJob",
    "PoolCompany", "PoolJob",
    "JobFitAssessment", "MatchAnalysis",
    "JobSeekerProfile", "ProfileEducation", "ProfileExperience",
    "AccomplishmentBullet", "CandidateProfile", "Certification", "Education", "Employment", "Project", "Skill",
    "TailoredBulletProvenance", "TailoredResume",
    "AICallLog",
    "AccountLink", "FreeAccount", "PersonMark",
    "ActivityEvent", "ActivityKind",
    "SchedulerRun", "SourceRunResult",
]
