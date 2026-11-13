from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CodeRules


def current_row(db: Session, code_id: int) -> CodeRules | None:
    return db.scalars(
        select(CodeRules).where(CodeRules.code_id == code_id).order_by(CodeRules.id.desc()).limit(1)
    ).first()


def current_rules(db: Session, code_id: int) -> tuple[list[str], list[str]]:
    """(include, exclude) from the code's newest rules row; empty lists with no row."""
    row = current_row(db, code_id)
    if row is None:
        return [], []
    return list(row.include or []), list(row.exclude or [])


def history(db: Session, code_id: int) -> list[CodeRules]:
    """Every rules row of the code, oldest first."""
    return list(db.scalars(select(CodeRules).where(CodeRules.code_id == code_id).order_by(CodeRules.id)))


def set_rules(
    db: Session,
    code_id: int,
    include: Sequence[str],
    exclude: Sequence[str],
    source: str,
    proposal_id: int | None = None,
) -> CodeRules:
    """Appends a new current rules row and commits."""
    row = CodeRules(
        code_id=code_id, include=list(include), exclude=list(exclude), source=source, proposal_id=proposal_id
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
