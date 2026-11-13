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
