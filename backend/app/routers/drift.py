from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import get_tagging_db
from ..schemas import DriftRequest
from ..services import drift

router = APIRouter(tags=["drift"])


@router.post("/codes/{code_id}/drift")
def draft(code_id: int, body: DriftRequest, db: Session = Depends(get_tagging_db)) -> dict:
    """Drafts a drift proposal from the code's disagreements (a paid OpenAI call, capped by
    drift.daily_token_cap), or returns the code's open suggested proposal without a call."""
    try:
        proposal = drift.draft(db, code_id, body.session_judgments, _LazyClient())
    except drift.CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} does not exist.") from exc
    except drift.CodeRetired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except drift.TokenCapReached as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except drift.DraftFailed as exc:
        raise HTTPException(
            status_code=502, detail={"message": f"Drafting failed: {exc}", "proposal_id": exc.proposal.id}
        ) from exc
    if proposal is None:
        return {"status": "no_disagreement"}
    return drift.serialize_proposal(proposal)


@router.get("/codes/{code_id}/proposals")
def proposals(code_id: int, db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        rows = drift.proposals_for(db, code_id)
    except drift.CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} does not exist.") from exc
    return [drift.serialize_proposal(p) for p in rows]


@router.get("/drift/spend")
def spend(db: Session = Depends(get_tagging_db)) -> dict:
    return {"today_tokens": drift.today_tokens(db), "cap_tokens": get_config().drift.daily_token_cap}


class _LazyClient:
    """Builds the OpenAI client only when a call is made, so a missing key never blocks the
    no-call paths (an open suggestion, no disagreement, the cap)."""

    def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]:
        return drift.OpenAIDriftClient().propose(prompt, schema)
