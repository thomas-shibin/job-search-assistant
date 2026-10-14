"""The CLI's database session helper (a regression: it once called itself)."""
from __future__ import annotations

import pytest
import typer


def test_cli_session_opens_and_binds_to_user(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.core.database.session as db
    import app.models  # noqa: F401
    import cli
    from app.core.database.base import Base
    from app.models.user import User
    from app.core.tenancy import tenant_of

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, info={"tenant_strict": True})
    with factory() as s:
        s.add(User(id=7, email="cli@example.com", first_name="C", last_name="L", role="member"))
        s.commit()
    monkeypatch.setattr(db, "SessionLocal", factory)

    monkeypatch.setattr(cli, "_acting_user_email", None)
    session = cli._session()
    assert tenant_of(session) is None
    session.close()

    monkeypatch.setattr(cli, "_acting_user_email", "CLI@example.com")
    session = cli._session()
    assert tenant_of(session) == 7
    session.close()

    monkeypatch.setattr(cli, "_acting_user_email", "nobody@example.com")
    with pytest.raises(typer.Exit):
        cli._session()
