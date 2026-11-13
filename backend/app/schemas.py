from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CodeEdit(BaseModel):
    """Fields left out are unchanged. `parent_code_id: null` moves a code to the top level,
    so callers must check `model_fields_set` rather than None to see what was sent."""

    name: str | None = None
    definition: str | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)
    parent_code_id: int | None = None
    status: Literal["ACTIVE", "RETIRED"] | None = None


class EmbedRequest(BaseModel):
    """The embedding job is paid, so it runs only when `confirm` is true."""

    confirm: bool = False


class RunRequest(BaseModel):
    """Leave `code_id` out for a sample run (random highlights judged against every active
    code); give it for a find-more run for that code."""

    code_id: int | None = None


class JudgmentRequest(BaseModel):
    """Accept, reject, Apply Code and Remove Code all post here; `applied` is the decision."""

    code_id: int
    highlight_ids: list[int] = Field(min_length=1)
    applied: bool


class HighlightStatusRequest(BaseModel):
    highlight_ids: list[int] = Field(min_length=1)
    status: Literal["not_started", "in_progress", "completed"]
