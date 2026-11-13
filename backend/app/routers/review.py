from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_cortico_db, get_tagging_db
from ..schemas import HighlightStatusRequest, JudgmentRequest
from ..services import judgments
from ..services.codebook_edit import CodeNotFound

router = APIRouter(tags=["review"])


def _code_404(code_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Code {code_id} not found.")


@router.get("/codes/{code_id}/review")
def review(
    code_id: int,
    run_id: int | None = None,
    db: Session = Depends(get_tagging_db),
    db_c: Session = Depends(get_cortico_db),
) -> dict:
    try:
        return judgments.review(db, db_c, code_id, run_id)
    except CodeNotFound as exc:
        raise _code_404(code_id) from exc


@router.get("/codes/{code_id}/agreement")
def agreement(code_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        return judgments.agreement(db, code_id)
    except CodeNotFound as exc:
        raise _code_404(code_id) from exc


@router.post("/judgments")
def judge(
    body: JudgmentRequest,
    db: Session = Depends(get_tagging_db),
    db_c: Session = Depends(get_cortico_db),
) -> dict:
    try:
        written = judgments.judge(db, body.code_id, body.highlight_ids, body.applied, db_c=db_c)
    except CodeNotFound as exc:
        raise _code_404(body.code_id) from exc
    except judgments.UnknownHighlights as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"written": written}


@router.post("/highlight_status")
def highlight_status(
    body: HighlightStatusRequest,
    db: Session = Depends(get_tagging_db),
    db_c: Session = Depends(get_cortico_db),
) -> dict:
    try:
        judgments.set_status(db, body.highlight_ids, body.status, db_c=db_c)
    except judgments.UnknownHighlights as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"updated": len(set(body.highlight_ids)), "status": body.status}
