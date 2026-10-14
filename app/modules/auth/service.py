"""Account rules: sign-up, sign-in, email verification, password reset,
guests, social sign-in and deleting an account.

Security choices, in one place:
- Responses never reveal whether an email has an account (sign-up, sign-in
  and "forgot password" all answer the same way; the owner gets an email).
- 5 wrong passwords lock the account for 15 minutes.
- A social sign-in only joins an existing account when the provider has
  verified that email address.
- Changing the password, or resetting it, signs out every other browser.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.modules.auth import emails as mailer
from app.modules.access import rbac
from app.modules.auth.passwords import hash_password, needs_rehash, password_problems, verify_password
from app.modules.auth.settings import AuthSettings
from app.modules.auth.tokens import consume_token, hash_secret, issue_token, revoke_tokens, utcnow
from app.modules.auth.models import AuthSession, AuthToken, OAuthIdentity, Role, TokenPurpose, User
from app.core.tenancy import TenantOwned, system_session
from app.core.logging import log_event

logger = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
GUEST_RETENTION = timedelta(days=7)

_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")
_NAME = re.compile(r"^[^\x00-\x1f<>\"]{1,100}$")


class AuthError(Exception):
    """Something the user can fix; the message is safe to show."""


@dataclass
class Problems:
    errors: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.errors)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def email_problem(value: str) -> str | None:
    value = normalize_email(value)
    if not value:
        return "Enter your email address."
    if len(value) > 320 or not _EMAIL.match(value):
        return "Enter a valid email address."
    return None


def name_problem(value: str, label: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return f"Enter your {label}."
    if not _NAME.match(value):
        return f"Your {label} can't contain < > or quotes, and must be under 100 characters."
    return None


def normalize_phone(value: str) -> str | None:
    """'+1 (604) 555-1234' / '604-555-1234' -> '+16045551234'; None if blank.
    Raises AuthError for something that isn't a phone number."""
    raw = (value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"[\s().-]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+1" + digits if len(digits) == 10 else "+" + digits  # 10 digits: North American number
    if not re.fullmatch(r"\+[1-9]\d{7,14}", digits):
        raise AuthError("Enter a valid phone number, e.g. +1 604 555 1234.")
    return digits


def account_for_email(session: Session, email: str) -> User | None:
    return _account_by_email(session, email)


def _account_by_email(session: Session, email: str) -> User | None:
    return session.query(User).filter(User.email == normalize_email(email), User.role != Role.GUEST.value).one_or_none()


def _role_for(email: str) -> str:
    return Role.ADMIN.value if normalize_email(email) in AuthSettings().admins else Role.MEMBER.value


def _link(path: str, token: str) -> str:
    return f"{AuthSettings().base_url}{path}?token={token}"


# ---- sign-up and verification ------------------------------------------------

def send_verification(session: Session, user: User, *, new_email: str | None = None) -> None:
    """Email a confirmation link -- to `new_email` when changing address."""
    revoke_tokens(session, user.id, TokenPurpose.VERIFY_EMAIL)
    token = issue_token(session, TokenPurpose.VERIFY_EMAIL, user_id=user.id, ttl=VERIFY_TTL,
                        payload={"email": normalize_email(new_email or user.email)})
    mailer.send_verification_email(new_email or user.email, user.first_name, _link("/verify-email", token))


def verify_email(session: Session, raw_token: str) -> User:
    token = consume_token(session, TokenPurpose.VERIFY_EMAIL, raw_token)
    user = session.get(User, token.user_id) if token else None
    if user is None:
        session.rollback()
        raise AuthError("That confirmation link is invalid or has expired. Sign in to get a new one.")
    address = (token.payload or {}).get("email") or user.email
    if address != user.email:  # confirming a changed address
        if _account_by_email(session, address) is not None:
            session.rollback()
            raise AuthError("That email address is already used by another account.")
        user.email = address
    user.email_verified_at = utcnow()
    if user.role == Role.MEMBER.value:
        user.role = _role_for(user.email)
    rbac.bootstrap_admin(session, user)
    session.commit()
    log_event(logger, "EMAIL_VERIFIED", user_id=user.id)
    return user


# ---- sign-in -------------------------------------------------------------------

def authenticate(session: Session, email: str, password: str) -> User:
    user = _account_by_email(session, email)
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        verify_password(password, None)  # same work either way
        raise AuthError("Too many failed attempts. Try again in a few minutes, or reset your password.")
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                user.failed_logins = 0
                log_event(logger, "ACCOUNT_LOCKED", user_id=user.id)
            session.commit()
        raise AuthError("Incorrect email or password.")
    user.failed_logins = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    session.commit()
    return user


def request_password_reset(session: Session, email: str) -> None:
    user = _account_by_email(session, email)
    if user is None or not user.is_active:
        return
    revoke_tokens(session, user.id, TokenPurpose.RESET_PASSWORD)
    token = issue_token(session, TokenPurpose.RESET_PASSWORD, user_id=user.id, ttl=RESET_TTL)
    session.commit()
    mailer.send_password_reset_email(user.email, user.first_name, _link("/reset-password", token))


def reset_password(session: Session, raw_token: str, new_password: str) -> User:
    token = consume_token(session, TokenPurpose.RESET_PASSWORD, raw_token)
    user = session.get(User, token.user_id) if token and token.user_id else None
    if user is None:
        session.rollback()
        raise AuthError("That reset link is invalid or has expired. Ask for a new one.")
    weak = password_problems(new_password, email=user.email, first_name=user.first_name, last_name=user.last_name)
    if weak:
        session.rollback()  # the link stays usable for a better password
        raise AuthError("Choose a stronger password: " + "; ".join(weak) + ".")
    user.password_hash = hash_password(new_password)
    user.failed_logins = 0
    user.locked_until = None
    if user.email_verified_at is None:  # they proved they read this inbox
        user.email_verified_at = utcnow()
    session.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    session.commit()
    log_event(logger, "PASSWORD_RESET", user_id=user.id)
    return user


def change_password(session: Session, user: User, current: str, new_password: str) -> None:
    if user.password_hash and not verify_password(current, user.password_hash):
        raise AuthError("Your current password is incorrect.")
    weak = password_problems(new_password, email=user.email, first_name=user.first_name, last_name=user.last_name)
    if weak:
        raise AuthError("Choose a stronger password: " + "; ".join(weak) + ".")
    user.password_hash = hash_password(new_password)
    session.commit()
    log_event(logger, "PASSWORD_CHANGED", user_id=user.id)


# ---- social sign-in ------------------------------------------------------------

@dataclass
class ProviderProfile:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    first_name: str
    last_name: str


def social_login(session: Session, profile: ProviderProfile) -> User:
    identity = session.query(OAuthIdentity).filter(
        OAuthIdentity.provider == profile.provider, OAuthIdentity.subject == profile.subject
    ).one_or_none()
    if identity is not None:
        user = session.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise AuthError("This account is disabled.")
        return user

    if not profile.email or not profile.email_verified:
        raise AuthError(
            f"Your {profile.provider.title()} account didn't share a verified email address. "
            "Sign up with your email instead."
        )
    user = _account_by_email(session, profile.email)
    if user is None:
        user = User(email=normalize_email(profile.email), first_name=(profile.first_name or "New")[:100],
                    last_name=(profile.last_name or "Member")[:100], role=_role_for(profile.email),
                    email_verified_at=utcnow())
        session.add(user)
        session.flush()
        rbac.grant_role(session, user, rbac.default_role_for(user.account_type))
        rbac.bootstrap_admin(session, user)
        log_event(logger, "SIGNUP_SOCIAL", user_id=user.id, provider=profile.provider)
    elif not user.is_active:
        raise AuthError("This account is disabled.")
    elif user.email_verified_at is None:
        # The provider proved the address; an unverified local password set by
        # someone else must not survive, or they'd share the account.
        user.email_verified_at = utcnow()
        user.password_hash = None
        session.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    session.add(OAuthIdentity(user_id=user.id, provider=profile.provider, subject=profile.subject,
                              email=normalize_email(profile.email)))
    session.commit()
    return user


# ---- account deletion and housekeeping ------------------------------------------

def _tenant_models() -> list[type]:
    import app.models  # noqa: F401  every model registered
    from app.core.database.base import Base

    return [m.class_ for m in Base.registry.mappers if issubclass(m.class_, TenantOwned)]


def delete_user_data(session: Session, user_id: int) -> None:
    """Every row any user-data table holds for `user_id` (no cascade needed)."""
    from app.modules.alerts.models import WatchlistAlert

    models = _tenant_models()
    # Children before parents: rows with foreign keys into other tenant tables first.
    pending = list(models)
    ordered: list[type] = []
    while pending:
        for model in list(pending):
            referenced_by_pending = any(
                fk.column.table is model.__table__ for other in pending if other is not model
                for fk in other.__table__.foreign_keys
            )
            if not referenced_by_pending:
                ordered.append(model)
                pending.remove(model)
                break
        else:  # a cycle: fall back to declared order
            ordered.extend(pending)
            break
    session.query(WatchlistAlert).filter(WatchlistAlert.user_id == user_id).update({WatchlistAlert.job_id: None})
    for model in ordered:
        session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)


def delete_account(session_factory, user_id: int) -> None:
    from app.modules.auth.storage import delete_user_files

    from app.modules.access.models import AuditLog, UserRole

    with system_session(session_factory) as session:
        if rbac.is_last_super_admin(session, user_id):
            raise AuthError("You're the only Super Admin. Make someone else a Super Admin before deleting "
                            "your account.")
        delete_user_data(session, user_id)
        session.query(UserRole).filter(UserRole.user_id == user_id).delete(synchronize_session=False)
        session.query(AuditLog).filter(AuditLog.actor_user_id == user_id).update(
            {AuditLog.actor_user_id: None}, synchronize_session=False)  # the record of what they did stays
        for model in (OAuthIdentity, AuthSession, AuthToken):
            session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
        session.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        session.commit()
    delete_user_files(user_id)
    log_event(logger, "ACCOUNT_DELETED", user_id=user_id)


ACTIVITY_RETENTION = timedelta(days=365)


def purge_expired(session_factory) -> None:
    """Expired sessions and tokens, activity older than a year, and guests older than a week."""
    with system_session(session_factory) as session:
        now = utcnow()
        session.query(AuthSession).filter(AuthSession.expires_at < now).delete(synchronize_session=False)
        session.query(AuthToken).filter(AuthToken.expires_at < now - timedelta(days=1)).delete(synchronize_session=False)
        from app.modules.usage.models import ActivityEvent

        session.query(ActivityEvent).filter(ActivityEvent.created_at < now - ACTIVITY_RETENTION).delete(
            synchronize_session=False)
        from app.modules.auth import otp

        otp.purge(session)
        old_guests = [uid for (uid,) in session.query(User.id).filter(
            User.role == Role.GUEST.value, User.created_at < now - GUEST_RETENTION)]
        session.commit()
    for uid in old_guests:
        delete_account(session_factory, uid)


# ---- sign-up and sign-in by emailed code (app/auth/otp.py, app/auth/api.py) -----------------

REGISTRATION_TTL = timedelta(minutes=20)


def account_problems(*, first_name: str, last_name: str, email: str, password: str,
                     confirm_password: str | None = None) -> Problems:
    problems = Problems()
    for key, problem in (("first_name", name_problem(first_name, "first name")),
                         ("last_name", name_problem(last_name, "last name"))):
        if problem:
            problems.errors[key] = problem
    weak = password_problems(password, email=email, first_name=first_name, last_name=last_name)
    if weak:
        problems.errors["password"] = "Choose a stronger password: " + "; ".join(weak) + "."
    if confirm_password is not None and password != confirm_password:
        problems.errors["confirm_password"] = "The passwords don't match."
    return problems


def start_registration(session: Session, email: str) -> str:
    """The email was just proven by a code: a single-use ticket (kept in an
    HttpOnly cookie) that lets this browser finish creating the account."""
    raw = issue_token(session, TokenPurpose.REGISTRATION, user_id=None, ttl=REGISTRATION_TTL,
                      payload={"email": normalize_email(email)})
    session.commit()
    return raw


def registration_email(session: Session, ticket: str | None) -> str:
    """The address a (still valid) registration ticket was issued for, or ""."""
    token = _registration_token(session, ticket)
    return (token.payload or {}).get("email", "") if token is not None else ""


def _registration_token(session: Session, ticket: str | None) -> AuthToken | None:
    if not ticket or len(ticket) > 200:
        return None
    token = session.query(AuthToken).filter(AuthToken.token_hash == hash_secret(ticket)).one_or_none()
    if (token is None or token.purpose != TokenPurpose.REGISTRATION.value or token.used_at is not None
            or token.expires_at < utcnow()):
        return None
    return token


def complete_registration(session: Session, ticket: str | None, *, first_name: str, last_name: str,
                          password: str, confirm_password: str) -> User:
    """Create the account the ticket's email was verified for. Raises AuthError
    (ticket missing / used / expired, or the address taken meanwhile)."""
    token = _registration_token(session, ticket)
    if token is None:
        raise AuthError("Your verification has expired. Start again with your email address.")
    email = (token.payload or {}).get("email", "")
    if _account_by_email(session, email) is not None:
        raise AuthError("An account with this email already exists. Sign in instead.")
    token.used_at = utcnow()
    user = User(email=email, first_name=first_name.strip(), last_name=last_name.strip(),
                password_hash=hash_password(password), role=_role_for(email), email_verified_at=utcnow(),
                account_type="job_seeker")
    session.add(user)
    session.flush()
    rbac.grant_role(session, user, rbac.default_role_for(user.account_type))  # never an admin role
    rbac.bootstrap_admin(session, user)
    session.commit()
    log_event(logger, "SIGNUP", user_id=user.id, method="email_code")
    mailer.send_welcome_email(user.email, user.first_name, f"{AuthSettings().base_url}/jobs")
    return user


def sign_in_with_code(session: Session, email: str) -> User | None:
    """The account for an email just proven by a code (now marked verified)."""
    user = _account_by_email(session, email)
    if user is None or not user.is_active:
        return None
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    user.failed_logins = 0
    user.locked_until = None
    session.commit()
    return user
