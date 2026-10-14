from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class CandidateProfile(TenantOwned, Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_line: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_resume_path: Mapped[str] = mapped_column(String(1024))
    # The uploaded file itself, so tailoring can always start from the exact
    # original design even if the file on disk is moved or lost.
    source_file: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_unparsed_sections: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    employments: Mapped[list["Employment"]] = relationship(
        back_populates="candidate_profile", cascade="all, delete-orphan"
    )
    skills: Mapped[list["Skill"]] = relationship(
        back_populates="candidate_profile", cascade="all, delete-orphan"
    )
    education: Mapped[list["Education"]] = relationship(
        back_populates="candidate_profile", cascade="all, delete-orphan"
    )
    certifications: Mapped[list["Certification"]] = relationship(
        back_populates="candidate_profile", cascade="all, delete-orphan"
    )
    projects: Mapped[list["Project"]] = relationship(
        back_populates="candidate_profile", cascade="all, delete-orphan"
    )


class Employment(TenantOwned, Base):
    __tablename__ = "employments"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    company: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    candidate_profile: Mapped["CandidateProfile"] = relationship(back_populates="employments")
    bullets: Mapped[list["AccomplishmentBullet"]] = relationship(
        back_populates="employment", cascade="all, delete-orphan"
    )


class AccomplishmentBullet(TenantOwned, Base):
    __tablename__ = "accomplishment_bullets"

    id: Mapped[int] = mapped_column(primary_key=True)
    employment_id: Mapped[int] = mapped_column(ForeignKey("employments.id"))
    text: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    employment: Mapped["Employment"] = relationship(back_populates="bullets")


class Skill(TenantOwned, Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    employment_id: Mapped[int | None] = mapped_column(ForeignKey("employments.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    candidate_profile: Mapped["CandidateProfile"] = relationship(back_populates="skills")


class Education(TenantOwned, Base):
    __tablename__ = "education"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    institution: Mapped[str] = mapped_column(String(255))
    degree: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_of_study: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    candidate_profile: Mapped["CandidateProfile"] = relationship(back_populates="education")


class Certification(TenantOwned, Base):
    __tablename__ = "certifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    name: Mapped[str] = mapped_column(String(255))
    issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    candidate_profile: Mapped["CandidateProfile"] = relationship(back_populates="certifications")


class Project(TenantOwned, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    candidate_profile: Mapped["CandidateProfile"] = relationship(back_populates="projects")
