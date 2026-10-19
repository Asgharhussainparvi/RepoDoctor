"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repodoctor.agent.schemas import SessionSnapshot

ID_PATTERN = r"^[0-9a-f]{12}$"
SHA_PATTERN = r"^[0-9a-f]{64}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(Strict):
    repository_id: str = Field(pattern=ID_PATTERN)


class PatchDecisionRequest(Strict):
    patch_sha256: str = Field(pattern=SHA_PATTERN)


class RepositoryView(BaseModel):
    id: str
    name: str
    kind: Literal["upload", "sample"]
    file_count: int
    total_bytes: int
    created_at: str
    files: list[str] = Field(
        default_factory=list
    )  # only filled by GET /api/repositories/{id}


class EngineInfo(BaseModel):
    kind: Literal["bedrock", "demo", "custom"]
    model_id: str | None = None
    simulated: bool = False  # True = scripted demo data, not a real model


class SessionView(SessionSnapshot):
    repository_id: str | None = None
    engine: EngineInfo


class ComponentStatus(BaseModel):
    ok: bool
    message: str


class HealthView(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    sandbox: ComponentStatus
    ai: ComponentStatus
    engine: EngineInfo
    active_sessions: int
    max_active_sessions: int
