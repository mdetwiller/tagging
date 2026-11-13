from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import get_tagging_db
from ..models import SearchRun
from ..schemas import RunRequest
from ..services import search_run

router = APIRouter(prefix="/runs", tags=["runs"])


def _serialize_run(run: SearchRun) -> dict:
    return {
        "id": run.id,
        "scope_code_id": run.scope_code_id,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "requests": run.requests,
        "tokens_used": run.tokens_used,
        "summary": run.summary,
    }


@router.post("", status_code=202)
def start(body: RunRequest | None = None) -> dict:
    try:
        run_id = search_run.start_run(body.code_id if body else None)
    except search_run.ScopeCodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except search_run.RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.get("/latest")
def latest(db: Session = Depends(get_tagging_db)) -> dict:
    run = db.scalars(select(SearchRun).order_by(SearchRun.id.desc()).limit(1)).first()
    if run is None:
        raise HTTPException(status_code=404, detail="No search runs yet.")
    return _serialize_run(run)


@router.get("/overview")
def overview(db: Session = Depends(get_tagging_db)) -> dict:
    """The configured sample size, the latest sample run, and the latest find-more run per code
    (keyed by code id as a string)."""
    sample = None
    find_more: dict[str, dict] = {}
    for run in db.scalars(select(SearchRun).order_by(SearchRun.id.desc())):
        if run.scope_code_id is None:
            if sample is None and (run.summary or {}).get("kind") == search_run.SAMPLE:
                sample = _serialize_run(run)
        else:
            find_more.setdefault(str(run.scope_code_id), _serialize_run(run))
    return {"sample_size": get_config().search.sample_size, "sample": sample, "find_more": find_more}


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    run = db.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Search run {run_id} does not exist.")
    return _serialize_run(run)
