from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Code, FamilyNote


class NoteCodeNotFound(LookupError):
    pass


class NotAFamily(ValueError):
    pass


def _family(db: Session, code_id: int) -> Code:
    code = db.get(Code, code_id)
    if code is None:
        raise NoteCodeNotFound(f"Code {code_id} not found.")
    if code.parent_code_id is not None:
        raise NotAFamily("Notes belong to a code family (a top-level code)")
    return code


def current_note(db: Session, code_id: int) -> FamilyNote | None:
    return db.scalars(
        select(FamilyNote).where(FamilyNote.code_id == code_id).order_by(FamilyNote.id.desc()).limit(1)
    ).first()


def current_notes(db: Session, code_ids: list[int]) -> dict[int, FamilyNote]:
    """The latest note of each code in code_ids that has one."""
    if not code_ids:
        return {}
    latest = (
        select(func.max(FamilyNote.id))
        .where(FamilyNote.code_id.in_(code_ids))
        .group_by(FamilyNote.code_id)
    )
    return {n.code_id: n for n in db.scalars(select(FamilyNote).where(FamilyNote.id.in_(latest)))}


def get_note(db: Session, code_id: int) -> FamilyNote | None:
    _family(db, code_id)
    return current_note(db, code_id)


def save_note(db: Session, code_id: int, text: str) -> FamilyNote | None:
    """Adds a row only when text differs from the current note."""
    _family(db, code_id)
    current = current_note(db, code_id)
    if (current.text if current else "") == text:
        return current
    note = FamilyNote(code_id=code_id, text=text, created_by=get_config().user_id)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note
