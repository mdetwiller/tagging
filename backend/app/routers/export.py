from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_cortico_db, get_tagging_db
from ..services.export import NoCodebook, build_export

router = APIRouter(tags=["export"])


@router.get("/export")
def export(db: Session = Depends(get_tagging_db), db_c: Session = Depends(get_cortico_db)) -> JSONResponse:
    try:
        body = build_export(db, db_c)
    except NoCodebook as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"tagging-export-{date.today().isoformat()}.json"
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
