from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Code, Codebook, CodeRevision, CodeRules, Coding, DriftProposal
from ..models_cortico import Highlight
from . import marks
from .family_notes import current_notes
from .judgments import agreement, latest_by_pair
from .search_run import code_instructions

DEVIATIONS = ["coding.highlight_id stored directly; highlight_share_id is null"]


class NoCodebook(LookupError):
    pass


def _iso(value: datetime | None) -> str | None:
    """SQLite drops the tzinfo of the UTC timestamps this app writes, so naive means UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def build_export(db_t: Session, db_c: Session) -> dict:
    """The current (newest) codebook, every coding of its codes, and per-code extras.
    `final` is set on one row per (code, highlight): the latest human row, else the latest AI row."""
    codebook = db_t.scalars(select(Codebook).order_by(Codebook.id.desc()).limit(1)).first()
    if codebook is None:
        raise NoCodebook("No codebook has been imported yet.")
    codes = list(
        db_t.scalars(select(Code).where(Code.codebook_id == codebook.id).order_by(Code.display_order, Code.id))
    )
    code_ids = [c.id for c in codes]
    by_id = {c.id: c for c in codes}

    rows = list(db_t.scalars(select(Coding).where(Coding.code_id.in_(code_ids)).order_by(Coding.id)))
    final_by_row: dict[int, bool | None] = {}
    for (code_id, _), latest in latest_by_pair(db_t, code_ids).items():
        row_id = latest.human_id if latest.human is not None else latest.ai_id
        final_by_row[row_id] = latest.final(by_id[code_id].threshold)

    human_ids = {r.highlight_id for r in rows if not r.is_ai_generated}
    texts = (
        dict(db_c.execute(select(Highlight.id, Highlight.text).where(Highlight.id.in_(human_ids))).all())
        if human_ids
        else {}
    )
    revisions: dict[int, list[dict]] = {c: [] for c in code_ids}
    for rev in db_t.scalars(select(CodeRevision).where(CodeRevision.code_id.in_(code_ids)).order_by(CodeRevision.id)):
        revisions[rev.code_id].append(
            {"field": rev.field, "before": rev.before, "after": rev.after, "created_at": _iso(rev.created_at)}
        )

    rules_history: dict[int, list[dict]] = {c: [] for c in code_ids}
    for row in db_t.scalars(select(CodeRules).where(CodeRules.code_id.in_(code_ids)).order_by(CodeRules.id)):
        rules_history[row.code_id].append(
            {
                "include": list(row.include or []),
                "exclude": list(row.exclude or []),
                "source": row.source,
                "proposal_id": row.proposal_id,
                "created_at": _iso(row.created_at),
            }
        )

    proposals: dict[int, list[dict]] = {c: [] for c in code_ids}
    for p in db_t.scalars(select(DriftProposal).where(DriftProposal.code_id.in_(code_ids)).order_by(DriftProposal.id)):
        proposals[p.code_id].append(
            {
                "id": p.id,
                "status": p.status,
                "base": p.base,
                "proposed": p.proposed,
                "applied": p.applied,
                "rationale": p.rationale,
                "score_summary": p.score_summary,
                "tokens": p.tokens,
                "model": (p.ai_metadata or {}).get("model"),
                "reasoning_effort": (p.ai_metadata or {}).get("reasoning_effort"),
                "input_tokens": (p.ai_metadata or {}).get("input_tokens"),
                "output_tokens": (p.ai_metadata or {}).get("output_tokens"),
                "disagreements": p.disagreements,
                "session_judgments": p.session_judgments,
                "created_at": _iso(p.created_at),
                "decided_at": _iso(p.decided_at),
            }
        )

    notes = current_notes(db_t, [c.id for c in codes if c.parent_code_id is None])

    earlier = db_t.execute(
        select(Codebook.id, Codebook.name, func.count(Coding.id))
        .outerjoin(Code, Code.codebook_id == Codebook.id)
        .outerjoin(Coding, Coding.code_id == Code.id)
        .where(Codebook.id < codebook.id)
        .group_by(Codebook.id, Codebook.name)
        .order_by(Codebook.id)
    ).all()

    return {
        "exported_at": _iso(datetime.now(timezone.utc)),
        "codebook": {
            "id": codebook.id,
            "name": codebook.name,
            "codes": [
                {
                    "id": c.id,
                    "name": c.name,
                    "description": c.definition,
                    "parent_id": c.parent_code_id,
                    "code_type": c.code_type,
                    "status": c.status,
                }
                for c in codes
            ],
        },
        "codings": [
            {
                "code_id": r.code_id,
                "highlight_id": r.highlight_id,
                "highlight_share_id": None,
                "conversation_id": r.conversation_id,
                "is_ai_generated": r.is_ai_generated,
                "ai_probability": r.ai_probability,
                "status": r.status,
                "created_by": r.created_by,
                "created_at": _iso(r.created_at),
                "final": final_by_row.get(r.id),
                "ai_metadata": r.ai_metadata,
            }
            for r in rows
        ],
        "x_tagging": {
            "codes": {
                str(c.id): {
                    "instructions": code_instructions(db_t, c, texts),
                    "threshold": c.threshold,
                    "revisions": revisions[c.id],
                    # Current rules are the newest history entry; with none, both lists are empty.
                    "rules": {
                        "include": rules_history[c.id][-1]["include"] if rules_history[c.id] else [],
                        "exclude": rules_history[c.id][-1]["exclude"] if rules_history[c.id] else [],
                    },
                    "rules_history": rules_history[c.id],
                    "agreement": agreement(db_t, c.id),
                    "proposals": proposals[c.id],
                    # Highlights the researcher starred as good examples; Jev always sees them.
                    "starred_highlight_ids": marks.starred_ids(db_t, c.id),
                }
                for c in codes
            },
            # Researcher notes on codes that may be missing from each family, keyed by parent code id.
            "family_notes": {
                str(code_id): {"name": by_id[code_id].name, "text": n.text, "updated_at": _iso(n.created_at)}
                for code_id, n in sorted(notes.items())
                if n.text.strip()
            },
            # Excluded from tagging, agreement and Jev examples; their codings above are kept.
            "facilitator_turns": [
                {"highlight_id": h, "marked_at": _iso(at)}
                for h, at in sorted(marks.facilitator_marked_at(db_t).items())
            ],
            "deviations": list(DEVIATIONS),
            # Re-importing starts a new codebook; codings on older ones are counted, not exported.
            "earlier_codebooks": [{"id": i, "name": n, "codings": count} for i, n, count in earlier],
        },
    }
