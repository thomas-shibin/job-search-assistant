from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class CandidateConfig(BaseModel):
    name: str
    base_resume: str


class AddressProfile(BaseModel):
    city: str
    province: str
    country: str


class SearchProfile(BaseModel):
    name: str
    enabled: bool = True
    locations: list[str] = Field(default_factory=list)
    address_profile: str
    titles: list[str] = Field(default_factory=list)


class MatchingWeights(BaseModel):
    technical_skills: int = 30
    relevant_experience: int = 20
    title_similarity: int = 15
    seniority: int = 10
    platform: int = 10
    location: int = 5
    domain_experience: int = 5
    education: int = 5

    @model_validator(mode="after")
    def weights_sum_to_100(self) -> "MatchingWeights":
        total = (
            self.technical_skills
            + self.relevant_experience
            + self.title_similarity
            + self.seniority
            + self.platform
            + self.location
            + self.domain_experience
            + self.education
        )
        if total != 100:
            raise ValueError(f"matching.weights must sum to 100, got {total}")
        return self


class MatchingConfig(BaseModel):
    minimum_score: int = 75
    required_or_high_value_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    weights: MatchingWeights = Field(default_factory=MatchingWeights)


class ResumeConfig(BaseModel):
    optimize_keywords: bool = True
    reorder_bullets: bool = True
    tailor_summary: bool = True
    tailor_skills: bool = True
    change_address: bool = True
    maximum_pages: int = 2
    never_invent_experience: bool = True


class NotificationsConfig(BaseModel):
    minimum_score: int = 80


class SchedulerConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 15


class AppConfig(BaseModel):
    candidate: CandidateConfig
    address_profiles: dict[str, AddressProfile] = Field(default_factory=dict)
    search_profiles: list[SearchProfile] = Field(default_factory=list)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
