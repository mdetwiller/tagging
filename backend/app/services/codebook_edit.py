from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Code, CodeRevision
from ..schemas import CodeEdit

# Fields whose change invalidates the code's search query.
_QUERY_FIELDS = ("name", "definition")
_FIELD_ORDER = ("name", "definition", "threshold", "parent_code_id", "status")
# These are columns that must never be null, so an explicit null for them is ignored.
_NON_NULL = ("name", "definition", "threshold", "status")


class CodeNotFound(LookupError):
    pass


class CodeEditError(ValueError):
    pass


def _check_move(db: Session, code: Code, parent_id: int | None) -> None:
    if parent_id is None:
        return
    if parent_id == code.id:
        raise CodeEditError("A code cannot be its own parent.")
    parent = db.get(Code, parent_id)
    if parent is None or parent.codebook_id != code.codebook_id:
        raise CodeEditError("The new parent must be a code in the same codebook.")
    if parent.parent_code_id is not None:
        raise CodeEditError("Codes have two levels at most; the new parent is itself a child.")
    has_children = db.scalar(select(exists().where(Code.parent_code_id == code.id)))
    if has_children:
        raise CodeEditError("A code that has children cannot be moved under another code.")


def edit_code(db: Session, code_id: int, changes: CodeEdit) -> Code:
    code = db.get(Code, code_id)
    if code is None:
        raise CodeNotFound(code_id)

    wanted: dict[str, Any] = {}
    for field in _FIELD_ORDER:
        if field not in changes.model_fields_set:
            continue
        value = getattr(changes, field)
        if value is None and field in _NON_NULL:
            continue
        if field == "name" and not value.strip():
            raise CodeEditError("A code name cannot be blank.")
        if getattr(code, field) != value:
            wanted[field] = value

    if "parent_code_id" in wanted:
        _check_move(db, code, wanted["parent_code_id"])

    user_id = get_config().user_id
    for field, after in wanted.items():
        db.add(
            CodeRevision(
                code_id=code.id,
                field=field,
                before=getattr(code, field),
                after=after,
                created_by=user_id,
            )
        )
        db.flush()
        setattr(code, field, after)
    if any(f in wanted for f in _QUERY_FIELDS):
        code.query_stale = True
    db.commit()
    db.refresh(code)
    return code


def list_revisions(db: Session, code_id: int) -> list[CodeRevision]:
    if db.get(Code, code_id) is None:
        raise CodeNotFound(code_id)
    return list(
        db.scalars(
            select(CodeRevision).where(CodeRevision.code_id == code_id).order_by(CodeRevision.id)
        )
    )
