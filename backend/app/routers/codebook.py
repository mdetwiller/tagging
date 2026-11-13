from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_tagging_db
from ..models import Code, CodeExemplar, Codebook
from ..services.codebook_import import CodebookFormatError, import_reconciled

router = APIRouter(prefix="/codebooks", tags=["codebooks"])


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
