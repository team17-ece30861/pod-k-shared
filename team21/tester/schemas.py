from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class TesterSetupRequest(BaseModel):
    target_url: HttpUrl


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)


class SessionResponse(BaseModel):
    session_id: str


class TestResult(BaseModel):
    test_id: str = Field(min_length=1)
    outcome: Literal["pass", "fail", "error", "skip"]
    message: str = Field(min_length=1)
    source: Literal["tester", "system", "communication", "unknown"] | None = None


class CoverageMeasure(BaseModel):
    covered: int | None
    total: int | None
    basis: str = Field(min_length=1)


class Coverage(BaseModel):
    code: CoverageMeasure
    component: CoverageMeasure
    system: CoverageMeasure


class SuiteReport(BaseModel):
    session_id: str
    target_url: str
    results: list[TestResult]
    coverage: Coverage
