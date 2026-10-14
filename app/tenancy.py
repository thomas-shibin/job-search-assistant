"""Multi-tenant isolation: every row of user data belongs to exactly one user,
and a database session can only ever see and change its own user's rows.

How it's enforced -- centrally, so no query has to remember to do it:

- Models holding user data mix in `TenantOwned` (a `user_id` column).
- A session is bound to one user with `bind_tenant(session, user_id)`. From
  then on SQLAlchemy adds ``WHERE user_id = :me`` to every ORM SELECT, UPDATE
  and DELETE touching a `TenantOwned` model -- including joins, subqueries,
  counts, relationship loads and `session.get()` (`_scope_queries`).
- On flush, new rows are stamped with the session's user, and a row owned by
  anyone else can't be inserted, changed or deleted (`_guard_writes`).
- Sessions from the app's own factory are *strict*: touching user data with no
  user bound raises `TenantContextError` instead of silently seeing everything
  (fail closed). Background work that genuinely spans users -- the scheduler
  picking whose searches to run, admin totals -- opts in explicitly with
  `system_session()`, which can read across users but never writes user rows
  without an explicit owner.

Raw SQL (`text()`) bypasses all of this, so application code never uses it on
user tables.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import ForeignKey, Integer, event
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column, with_loader_criteria

_TENANT = "tenant_user_id"
_SYSTEM = "tenant_system"
_STRICT = "tenant_strict"

# Tests only: rows created by a non-strict session with no user bound are
# stamped with this owner, so fixtures written before multi-tenancy keep
# working. Always None in the running app (its sessions are strict).
unscoped_owner_id: int | None = None


class TenantContextError(RuntimeError):
    """User data was touched with no user bound to the session (or across users)."""


class TenantOwned:
    """Mixin for every model that holds a user's data."""

    @declared_attr
    def user_id(cls) -> Mapped[int | None]:
        # Nullable only so the schema can be migrated in place; the write guard
        # below never lets the app store an unowned row.
        return mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)


def bind_tenant(session: Session, user_id: int) -> Session:
    if not isinstance(user_id, int) or user_id <= 0:
        raise TenantContextError(f"invalid tenant id {user_id!r}")
    current = session.info.get(_TENANT)
    if current is not None and current != user_id:
        raise TenantContextError("a session can't be re-bound to a different user")
    session.info[_TENANT] = user_id
    return session


def make_strict(session: Session) -> Session:
    session.info[_STRICT] = True
    return session


def tenant_of(session: Session) -> int | None:
    return session.info.get(_TENANT)


def require_tenant(session: Session) -> int:
    user_id = tenant_of(session)
    if user_id is None:
        raise TenantContextError("this operation needs a signed-in user")
    return user_id


@contextmanager
def system_session(factory) -> Iterator[Session]:
    """A cross-user session for platform work (scheduling, admin totals)."""
    session = factory()
    session.info[_SYSTEM] = True
    try:
        yield session
    finally:
        session.close()


@contextmanager
def tenant_session(factory, user_id: int) -> Iterator[Session]:
    """A session that sees only `user_id`'s data (e.g. a background scan for one user)."""
    session = make_strict(bind_tenant(factory(), user_id))
    try:
        yield session
    finally:
        session.close()


def _touches_tenant_data(state) -> bool:
    return any(issubclass(m.class_, TenantOwned) for m in state.all_mappers)


@event.listens_for(Session, "do_orm_execute")
def _scope_queries(state) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    session = state.session
    user_id = session.info.get(_TENANT)
    if user_id is None:
        if session.info.get(_SYSTEM) or not session.info.get(_STRICT):
            return
        if _touches_tenant_data(state):
            raise TenantContextError("user data queried with no signed-in user")
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantOwned, lambda cls: cls.user_id == user_id, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _guard_writes(session: Session, flush_context, instances) -> None:
    user_id = session.info.get(_TENANT)
    system = session.info.get(_SYSTEM)
    strict = session.info.get(_STRICT)

    for obj in session.new:
        if not isinstance(obj, TenantOwned):
            continue
        if user_id is not None:
            if obj.user_id is None:
                obj.user_id = user_id
            elif obj.user_id != user_id:
                raise TenantContextError("can't create a row for another user")
        elif obj.user_id is None:
            if system or strict or unscoped_owner_id is None:
                raise TenantContextError(f"new {type(obj).__name__} has no owner")
            obj.user_id = unscoped_owner_id

    if user_id is None:
        return
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, TenantOwned) and obj.user_id is not None and obj.user_id != user_id:
            raise TenantContextError("can't change another user's data")


def is_system(session: Session) -> bool:
    return bool(session.info.get(_SYSTEM))


def sees_user_data(session: Session) -> bool:
    """True when this session reads exactly one user's data: bound to a user,
    or (tests / single-user tools only) a plain non-strict, non-system session."""
    if tenant_of(session) is not None:
        return True
    return not (session.info.get(_SYSTEM) or session.info.get(_STRICT))
