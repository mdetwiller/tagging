from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from .db import Base

# highlight_id and conversation_id refer to the Cortico snapshot, a separate database, so
# they carry no foreign keys. Rows in code, code_revision, coding and highlight_status are
# never deleted.


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Codebook(Base):
    __tablename__ = "codebook"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    source_file = Column(String)
    source_last_saved = Column(String)
    imported_at = Column(DateTime, nullable=False, default=_now)
    status = Column(String, nullable=False, default="ACTIVE")


class Code(Base):
    __tablename__ = "code"

    id = Column(Integer, primary_key=True)
    codebook_id = Column(Integer, ForeignKey("codebook.id"), nullable=False, index=True)
    parent_code_id = Column(Integer, ForeignKey("code.id"), index=True)
    name = Column(String, nullable=False)
    definition = Column(Text, nullable=False, default="")
    code_type = Column(String, nullable=False, default="THEMATIC")
    status = Column(String, nullable=False, default="ACTIVE")  # ACTIVE | RETIRED
    threshold = Column(Float, nullable=False, default=0.5)
    display_order = Column(Integer, nullable=False, default=0)
    origin_id = Column(String)
    source = Column(String)
    x_source = Column(JSON)
    query_stale = Column(Boolean, nullable=False, default=True)


class CodeRevision(Base):
    __tablename__ = "code_revision"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    field = Column(String, nullable=False)
    before = Column(JSON)
    after = Column(JSON)
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)


class CodeExemplar(Base):
    __tablename__ = "code_exemplar"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    snippet_id = Column(Integer)
    conversation_id = Column(Integer)
    text = Column(Text, nullable=False)
    source = Column(String, nullable=False, default="codestormer")


class Coding(Base):
    __tablename__ = "coding"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    highlight_id = Column(Integer, nullable=False, index=True)
    conversation_id = Column(Integer, nullable=False)
    highlight_share_id = Column(Integer)
    is_ai_generated = Column(Boolean, nullable=False, default=False)
    ai_probability = Column(Float)
    status = Column(String, nullable=False)  # suggested | applied | not_applied
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    run_id = Column(Integer, ForeignKey("search_run.id"), index=True)
    ai_metadata = Column(JSON)


class HighlightStatus(Base):
    __tablename__ = "highlight_status"

    highlight_id = Column(Integer, primary_key=True)
    status = Column(String, nullable=False, default="not_started")  # not_started | in_progress | completed
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SearchRun(Base):
    __tablename__ = "search_run"

    id = Column(Integer, primary_key=True)
    scope_code_id = Column(Integer, ForeignKey("code.id"))
    status = Column(String, nullable=False, default="running")  # running | done | failed
    started_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime)
    requests = Column(Integer, nullable=False, default=0)
    tokens_used = Column(Integer, nullable=False, default=0)
    summary = Column(JSON)


class RunHighlight(Base):
    __tablename__ = "run_highlight"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("search_run.id"), nullable=False, index=True)
    target_code_id = Column(Integer, ForeignKey("code.id"), nullable=False)
    highlight_id = Column(Integer, nullable=False)
    picked_by = Column(String, nullable=False)  # similarity | random
    position = Column(Integer, nullable=False)


class FamilyNote(Base):
    """Append-only: the current note for a code is its row with the highest id. Rows are never
    deleted. Only top-level codes (code families) have notes."""

    __tablename__ = "family_note"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    text = Column(Text, nullable=False, default="")
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)


class CodeRules(Base):
    """Append-only: a code's current include/exclude rules are its row with the highest id;
    with no row, both lists are empty. Rows are never deleted."""

    __tablename__ = "code_rules"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    include = Column(JSON, nullable=False, default=list)
    exclude = Column(JSON, nullable=False, default=list)
    source = Column(String, nullable=False)  # proposal | rollback | user | restore
    proposal_id = Column(Integer)  # the drift_proposal that set these rules, if any
    created_at = Column(DateTime, nullable=False, default=_now)


class DriftProposal(Base):
    """`base` and `proposed` are {name, definition, threshold, include, exclude}; `proposed`
    holds only the fields that change. Rows are never deleted."""

    __tablename__ = "drift_proposal"

    id = Column(Integer, primary_key=True)
    code_id = Column(Integer, ForeignKey("code.id"), nullable=False, index=True)
    # suggested | accepted | dismissed | rolled_back | no_change | failed
    status = Column(String, nullable=False, default="suggested")
    base = Column(JSON, nullable=False)
    proposed = Column(JSON, nullable=False)
    applied = Column(JSON)
    rationale = Column(Text, nullable=False, default="")
    disagreements = Column(JSON, nullable=False, default=list)
    session_judgments = Column(Integer, nullable=False, default=0)
    ai_metadata = Column(JSON)
    tokens = Column(Integer, nullable=False, default=0)
    score_status = Column(String, nullable=False, default="none")  # none | running | done | failed
    score_summary = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=_now)
    decided_at = Column(DateTime)


class ProposalScore(Base):
    """Rows are never deleted."""

    __tablename__ = "proposal_score"

    id = Column(Integer, primary_key=True)
    proposal_id = Column(Integer, ForeignKey("drift_proposal.id"), nullable=False, index=True)
    highlight_id = Column(Integer, nullable=False)
    human = Column(String, nullable=False)  # applied | not_applied
    current_p = Column(Float)
    proposed_p = Column(Float)
    created_at = Column(DateTime, nullable=False, default=_now)


class HighlightMark(Base):
    """A facilitator-turn mark (code_id null, applies to every code) or a star for one code.
    Rows are never deleted; the newest row per (kind, highlight_id, code_id) is the current
    state."""

    __tablename__ = "highlight_mark"

    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)  # facilitator | star
    highlight_id = Column(Integer, nullable=False, index=True)
    code_id = Column(Integer, ForeignKey("code.id"), index=True)
    active = Column(Boolean, nullable=False)
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)


class CodebookVersion(Base):
    """A saved copy of a codebook's codes and rules, to restore later. Rows are never deleted."""

    __tablename__ = "codebook_version"

    id = Column(Integer, primary_key=True)
    codebook_id = Column(Integer, ForeignKey("codebook.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    # {"codes": {"<code_id>": {name, definition, threshold, parent_code_id, status, include, exclude}}}
    snapshot = Column(JSON, nullable=False)
    source = Column(String, nullable=False)  # user | before_restore
    restored_version_id = Column(Integer)  # for before_restore: the version being restored
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)
