from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_tagging_db
from ..models import Code, CodeExemplar, Codebook
from ..schemas import CodeEdit
from ..services.codebook_edit import CodeEditError, CodeNotFound, edit_code, list_revisions
from ..services.codebook_import import CodebookFormatError, import_reconciled

router = APIRouter(prefix="/codebooks", tags=["codebooks"])
codes_router = APIRouter(prefix="/codes", tags=["codes"])


@router.post("/import")
async def import_codebook(
    file: UploadFile = File(...), db: Session = Depends(get_tagging_db)
) -> dict:
    raw = await file.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"File is not valid JSON: {exc}") from exc
    try:
        return dict(import_reconciled(db, data, file.filename or "upload.json"))
    except CodebookFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _serialize_code(code: Code, exemplar_counts: dict[int, int], children: list[dict]) -> dict:
    return {
        "id": code.id,
        "name": code.name,
        "definition": code.definition,
        "parent_code_id": code.parent_code_id,
        "status": code.status,
        "threshold": code.threshold,
        "display_order": code.display_order,
        "exemplar_count": exemplar_counts.get(code.id, 0),
        "children": children,
    }


@router.get("/current")
def current_codebook(db: Session = Depends(get_tagging_db)) -> dict:
    codebook = db.scalars(select(Codebook).order_by(Codebook.id.desc()).limit(1)).first()
    if codebook is None:
        raise HTTPException(status_code=404, detail="No codebook has been imported yet.")
    codes = list(
        db.scalars(
            select(Code)
            .where(Code.codebook_id == codebook.id)
            .order_by(Code.display_order, Code.id)
        )
    )
    exemplar_counts = dict(
        db.execute(
            select(CodeExemplar.code_id, func.count(CodeExemplar.id))
            .join(Code, Code.id == CodeExemplar.code_id)
            .where(Code.codebook_id == codebook.id)
            .group_by(CodeExemplar.code_id)
        ).all()
    )
    children_of: dict[int, list[Code]] = {}
    for code in codes:
        if code.parent_code_id is not None:
            children_of.setdefault(code.parent_code_id, []).append(code)
    tree = [
        _serialize_code(
            parent,
            exemplar_counts,
            [_serialize_code(c, exemplar_counts, []) for c in children_of.get(parent.id, [])],
        )
        for parent in codes
        if parent.parent_code_id is None
    ]
    return {"id": codebook.id, "name": codebook.name, "codes": tree}


@codes_router.patch("/{code_id}")
def patch_code(code_id: int, changes: CodeEdit, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        code = edit_code(db, code_id, changes)
    except CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} not found.") from exc
    except CodeEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    children = list(
        db.scalars(
            select(Code)
            .where(Code.parent_code_id == code.id)
            .order_by(Code.display_order, Code.id)
        )
    )
    ids = [code.id] + [c.id for c in children]
    exemplar_counts = dict(
        db.execute(
            select(CodeExemplar.code_id, func.count(CodeExemplar.id))
            .where(CodeExemplar.code_id.in_(ids))
            .group_by(CodeExemplar.code_id)
        ).all()
    )
    return _serialize_code(
        code, exemplar_counts, [_serialize_code(c, exemplar_counts, []) for c in children]
    )


@codes_router.get("/{code_id}/revisions")
def code_revisions(code_id: int, db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        revisions = list_revisions(db, code_id)
    except CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} not found.") from exc
    return [
        {
            "field": r.field,
            "before": r.before,
            "after": r.after,
            "created_at": r.created_at.isoformat(),
        }
        for r in revisions
    ]
