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


class FamilyNoteRequest(BaseModel):
    """The whole note; it replaces the current one (earlier versions are kept).
    `base_updated_at` is the `updated_at` the client loaded (null for no note yet). When it is
    sent and the note has changed since, the save is refused, so callers must check
    `model_fields_set` to tell a sent null from an omitted field."""

    text: str = Field(max_length=20000)
    base_updated_at: str | None = None


class DriftRequest(BaseModel):
    """`session_judgments` is how many highlights the researcher judged for the code in the
    review session that just ended; it is stored on the proposal."""

    session_judgments: int = Field(ge=0)


class ProposalValues(BaseModel):
    """The researcher's edits at accept; each wins over the proposal's value. Ranges and
    duplicates are checked by the service (400), so these are left unconstrained here."""

    model_config = {"extra": "forbid"}

    name: str | None = None
    definition: str | None = None
    threshold: float | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None


class AcceptProposalRequest(BaseModel):
    """`rules` covers both include and exclude; a list left out of both `proposed` and
    `values` keeps its current value."""

    parts: list[Literal["name", "definition", "threshold", "rules"]]
    values: ProposalValues | None = None
