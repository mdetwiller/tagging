from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import get_tagging_db
from ..schemas import AcceptProposalRequest, DriftRequest
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
    return drift.serialize_proposal(db, proposal)


@router.get("/codes/{code_id}/proposals")
def proposals(code_id: int, db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        rows = drift.proposals_for(db, code_id)
    except drift.CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} does not exist.") from exc
    return [drift.serialize_proposal(db, p) for p in rows]


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        return drift.serialize_proposal(db, drift.get_proposal(db, proposal_id))
    except drift.ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/score", status_code=202)
def score(proposal_id: int) -> dict:
    """Test with Jev: scores the suggested proposal against the code's human-judged
    highlights in the background (one Jev request per highlight). 409 when the proposal is not
    suggested, its code is not active, or any Jev job is running. Poll GET /proposals/{id}."""
    try:
        drift.start_score(proposal_id)
    except drift.ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except drift.ScoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"proposal_id": proposal_id, "score_status": "running"}


@router.post("/proposals/{proposal_id}/accept")
def accept(proposal_id: int, body: AcceptProposalRequest, db: Session = Depends(get_tagging_db)) -> dict:
    """Applies the chosen parts of a suggested proposal; `values` are the researcher's edits.
    409 when the proposal is not suggested, is being scored, or a chosen field has changed
    since the draft."""
    values = body.values.model_dump(exclude_unset=True) if body.values else None
    return _decide(db, lambda: drift.accept(db, proposal_id, list(body.parts), values))


@router.post("/proposals/{proposal_id}/dismiss")
def dismiss(proposal_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    return _decide(db, lambda: drift.dismiss(db, proposal_id))


@router.post("/proposals/{proposal_id}/rollback")
def rollback(proposal_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    """Restores the code's values from before its newest accepted proposal. 409 when a field
    the proposal applied has been changed since, or a proposal for the code is being scored."""
    return _decide(db, lambda: drift.rollback(db, proposal_id))


def _decide(db: Session, action) -> dict:
    try:
        return drift.serialize_proposal(db, action())
    except drift.ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except drift.DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except drift.DecisionInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/drift/spend")
def spend(db: Session = Depends(get_tagging_db)) -> dict:
    return {"today_tokens": drift.today_tokens(db), "cap_tokens": get_config().drift.daily_token_cap}


class _LazyClient:
    """Builds the OpenAI client only when a call is made, so a missing key never blocks the
    no-call paths (an open suggestion, no disagreement, the cap)."""

    def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]:
        return drift.OpenAIDriftClient().propose(prompt, schema)
