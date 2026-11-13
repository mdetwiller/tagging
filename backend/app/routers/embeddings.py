from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_cortico_db, get_tagging_db
from ..schemas import EmbedRequest
from ..services import embeddings

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


@router.get("/status")
def status(
    db_c: Session = Depends(get_cortico_db), db_t: Session = Depends(get_tagging_db)
) -> dict:
    return embeddings.estimate(db_c, db_t)


@router.post("")
def run_embeddings(
    body: EmbedRequest | None = None,
    db_c: Session = Depends(get_cortico_db),
    db_t: Session = Depends(get_tagging_db),
) -> dict:
    if body is None or not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Embedding is a paid call. Check GET /embeddings/status, then send {\"confirm\": true}.",
        )
    try:
        client = embeddings.OpenAIEmbeddingClient()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return embeddings.embed_all(db_c, db_t, client)
