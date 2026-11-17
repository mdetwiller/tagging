from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import HighlightMark

FACILITATOR = "facilitator"
STAR = "star"


def _current(db: Session, kind: str, code_id: int | None = None) -> dict[int, HighlightMark]:
    """highlight_id -> newest mark row of this kind (for code_id when kind is star)."""
    query = select(HighlightMark).where(HighlightMark.kind == kind).order_by(HighlightMark.id)
    if kind == STAR:
        query = query.where(HighlightMark.code_id == code_id)
    return {m.highlight_id: m for m in db.scalars(query)}


def facilitator_ids(db: Session) -> set[int]:
    return {h for h, m in _current(db, FACILITATOR).items() if m.active}


def facilitator_marked_at(db: Session) -> dict[int, object]:
    """highlight_id -> when it was marked, for current facilitator turns."""
    return {h: m.created_at for h, m in _current(db, FACILITATOR).items() if m.active}


def starred_ids(db: Session, code_id: int) -> list[int]:
    """The code's currently starred highlights, oldest star first."""
    current = [m for m in _current(db, STAR, code_id).values() if m.active]
    return [m.highlight_id for m in sorted(current, key=lambda m: m.id)]


def write(db: Session, kind: str, highlight_ids: Iterable[int], active: bool, code_id: int | None = None) -> int:
    """Appends a row for each highlight whose current state differs; returns rows added.
    The caller commits."""
    current = _current(db, kind, code_id)
    user_id = get_config().user_id
    added = 0
    for hid in dict.fromkeys(highlight_ids):
        mark = current.get(hid)
        if (mark.active if mark else False) == active:
            continue
        db.add(HighlightMark(kind=kind, highlight_id=hid, code_id=code_id, active=active, created_by=user_id))
        added += 1
    return added
