from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import CorticoSession
from ..models import Code, DriftProposal
from ..models_cortico import Highlight
from . import judgments, rules
from .codebook_edit import CodeNotFound

MAX_RULES = 8
ESTIMATED_OUTPUT_TOKENS = 1000

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "definition": {"type": ["string", "null"]},
        "threshold": {"type": ["number", "null"]},
        "include": {"type": "array", "items": {"type": "string"}},
        "exclude": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["name", "definition", "threshold", "include", "exclude", "rationale"],
    "additionalProperties": False,
}


class CodeRetired(ValueError):
    pass


class TokenCapReached(RuntimeError):
    pass


class DriftCallError(RuntimeError):
    """A call that failed after the model had used tokens (for example, unparseable output)."""

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens, self.output_tokens = input_tokens, output_tokens


class DraftFailed(RuntimeError):
    def __init__(self, proposal: DriftProposal, message: str) -> None:
        super().__init__(message)
        self.proposal = proposal


class DriftClient(Protocol):
    def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]:
        """The parsed JSON output, input tokens and output tokens."""
        ...


class OpenAIDriftClient:
    """Paid. Never construct this in tests."""

    def __init__(self) -> None:
        config = get_config()
        api_key = config.openai_api_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment or backend/.env.")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=config.openai_base_url() or None)
        self._model = config.drift.model
        self._effort = config.drift.reasoning_effort

    def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]:
        response = self._client.responses.create(
            model=self._model,
            reasoning={"effort": self._effort},
            input=prompt,
            text={"format": {"type": "json_schema", "name": "drift_proposal", "schema": schema, "strict": True}},
        )
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        try:
            output = json.loads(response.output_text)
        except (TypeError, ValueError) as exc:
            raise DriftCallError(f"Unparseable model output: {exc}", input_tokens, output_tokens) from exc
        return output, input_tokens, output_tokens


def estimate_tokens(prompt: str) -> int:
    return len(prompt) // 4 + ESTIMATED_OUTPUT_TOKENS


def _local_day_utc(now: datetime | None) -> tuple[datetime, datetime]:
    """[start, end) of now's local calendar day, as naive UTC to match stored created_at.
    A naive `now` is taken as local time."""
    local = (now or datetime.now()).astimezone()
    start = datetime.combine(local.date(), time()).astimezone()
    end = datetime.combine(local.date() + timedelta(days=1), time()).astimezone()
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def today_tokens(db: Session, now: datetime | None = None) -> int:
    """Tokens of every proposal (any status) created on now's local calendar day."""
    start, end = _local_day_utc(now)
    total = db.scalar(
        select(func.coalesce(func.sum(DriftProposal.tokens), 0)).where(
            DriftProposal.created_at >= start, DriftProposal.created_at < end
        )
    )
    return int(total or 0)


def _base(db: Session, code: Code) -> dict:
    include, exclude = rules.current_rules(db, code.id)
    return {
        "name": code.name, "definition": code.definition, "threshold": code.threshold,
        "include": include, "exclude": exclude,
    }


def _judged_examples(db: Session, code: Code, disagreeing: list[int]) -> list[tuple[int, str, float | None]]:
    """(highlight_id, human verdict, AI probability) for human-judged highlights, disagreements
    first, then the newest other judgments, at most max_judged_examples."""
    latest = {hid: l for (_, hid), l in judgments.latest_by_pair(db, [code.id]).items() if l.human is not None}
    disagreeing_set = set(disagreeing)
    others = sorted((h for h in latest if h not in disagreeing_set), key=lambda h: -latest[h].human_id)
    ordered = [h for h in disagreeing if h in latest] + others
    limit = get_config().drift.max_judged_examples
    return [(h, latest[h].human, latest[h].ai_probability) for h in ordered[:limit]]


def build_prompt(
    db: Session, code: Code, db_c: Session | None = None, disagreeing: list[int] | None = None
) -> str:
    """`disagreeing` is the code's disagreements() when the caller already has them."""
    include, exclude = rules.current_rules(db, code.id)
    parent = db.get(Code, code.parent_code_id) if code.parent_code_id else None
    siblings = list(
        db.scalars(
            select(Code)
            .where(
                Code.codebook_id == code.codebook_id,
                Code.parent_code_id.is_(None) if code.parent_code_id is None else Code.parent_code_id == code.parent_code_id,
                Code.status == "ACTIVE",
                Code.id != code.id,
            )
            .order_by(Code.display_order, Code.id)
        )
    )
    if disagreeing is None:
        disagreeing = judgments.disagreements(db, code.id)
    examples = _judged_examples(db, code, disagreeing)

    def load_texts(s: Session) -> dict[int, str]:
        ids = [h for h, _, _ in examples]
        return dict(s.execute(select(Highlight.id, Highlight.text).where(Highlight.id.in_(ids))).all()) if ids else {}

    if db_c is not None:
        texts = load_texts(db_c)
    else:
        with CorticoSession() as s:
            texts = load_texts(s)

    def bullet(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "(none)"

    lines = [
        "You maintain one code in a qualitative codebook. An AI tagger applies the code to highlights",
        "from recorded community conversations; a researcher then accepts or rejects each call. Below are",
        "the code, its neighbours, and highlights the researcher judged, with the tagger's probability.",
        "Propose changes so the tagger's calls match the researcher's verdicts, especially on the",
        "disagreements, without overlapping the neighbouring codes.",
        "",
        "Rules describe what the code includes or excludes in general terms, not specific highlights;",
        "never quote or point to a highlight in a rule. Return include and exclude as the full new lists,",
        f"at most {MAX_RULES} each; an empty list leaves that list's rules unchanged. Return null for name,",
        "definition or threshold to leave it unchanged. The threshold is the probability (0 to 1) at or",
        "above which the tagger's call counts as yes. Explain your reasoning briefly in rationale.",
        "",
        "## Code",
        f"Name: {code.name}",
        f"Definition: {code.definition}",
        f"Threshold: {code.threshold}",
        "Include rules:",
        bullet(include),
        "Exclude rules:",
        bullet(exclude),
        "",
        "## Neighbours",
        f"Parent: {parent.name if parent else '(none; this is a top-level code)'}",
        "Siblings:",
        bullet([f"{s.name}: {s.definition}" for s in siblings]),
        "",
        f"## Judged highlights ({len(disagreeing)} disagreements listed first)",
    ]
    disagreeing_set = set(disagreeing)
    for i, (hid, human, p) in enumerate(examples, 1):
        verdict = "applies" if human == "applied" else "does not apply"
        prob = "none" if p is None else f"{p:.2f}"
        tag = " [DISAGREEMENT]" if hid in disagreeing_set else ""
        lines += [f"{i}.{tag} Researcher: {verdict}. Tagger probability: {prob}.", f"   Text: {texts.get(hid, '')}"]
    return "\n".join(lines)


def _clean_rules(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [s.strip() for s in value if isinstance(s, str) and s.strip()][:MAX_RULES]


def _validate(db: Session, code: Code, base: dict, output: dict) -> dict:
    """Only the fields that pass validation and differ from `base`."""
    proposed: dict = {}

    name = output.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != base["name"]:
        taken = {
            n.strip().casefold()
            for n in db.scalars(
                select(Code.name).where(
                    Code.codebook_id == code.codebook_id, Code.status == "ACTIVE", Code.id != code.id
                )
            )
        }
        if name.strip().casefold() not in taken:
            proposed["name"] = name.strip()

    definition = output.get("definition")
    if isinstance(definition, str) and definition.strip() and definition.strip() != base["definition"]:
        proposed["definition"] = definition.strip()

    threshold = output.get("threshold")
    if (
        isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
        and 0 <= threshold <= 1
        and float(threshold) != base["threshold"]
    ):
        proposed["threshold"] = float(threshold)

    for field in ("include", "exclude"):
        cleaned = _clean_rules(output.get(field))
        if cleaned and cleaned != base[field]:
            proposed[field] = cleaned
    return proposed


def _suggested(db: Session, code_id: int) -> DriftProposal | None:
    return db.scalars(
        select(DriftProposal)
        .where(DriftProposal.code_id == code_id, DriftProposal.status == "suggested")
        .order_by(DriftProposal.id.desc())
        .limit(1)
    ).first()


def draft(
    db: Session, code_id: int, session_judgments: int, client: DriftClient, now: datetime | None = None
) -> DriftProposal | None:
    """Drafts a proposal for the code, or returns its open `suggested` one without a call.
    None means the code has no disagreement, so nothing was called or saved."""
    code = db.get(Code, code_id)
    if code is None:
        raise CodeNotFound(code_id)
    if code.status == "RETIRED":
        raise CodeRetired(f"Code {code_id} is retired.")

    existing = _suggested(db, code_id)
    if existing is not None:
        return existing

    disagreeing = judgments.disagreements(db, code_id)
    if not disagreeing:
        return None

    config = get_config().drift
    prompt = build_prompt(db, code, disagreeing=disagreeing)
    if today_tokens(db, now) + estimate_tokens(prompt) > config.daily_token_cap:
        raise TokenCapReached("Daily drift token limit reached")

    created_at = (now or datetime.now()).astimezone(timezone.utc)
    base = _base(db, code)
    metadata = {"model": config.model, "reasoning_effort": config.reasoning_effort, "prompt": prompt}
    proposal = DriftProposal(
        code_id=code_id, base=base, disagreements=disagreeing, session_judgments=session_judgments,
        created_at=created_at,
    )
    try:
        output, input_tokens, output_tokens = client.propose(prompt, OUTPUT_SCHEMA)
    except Exception as exc:  # noqa: BLE001  any client failure is recorded, then reported as 502
        proposal.status = "failed"
        proposal.proposed = {}
        used_in = int(getattr(exc, "input_tokens", 0) or 0)
        used_out = int(getattr(exc, "output_tokens", 0) or 0)
        proposal.tokens = used_in + used_out
        proposal.ai_metadata = {
            **metadata, "input_tokens": used_in, "output_tokens": used_out, "error": f"{type(exc).__name__}: {exc}",
        }
        db.add(proposal)
        db.commit()
        db.refresh(proposal)
        raise DraftFailed(proposal, str(exc)) from exc

    proposed = _validate(db, code, base, output if isinstance(output, dict) else {})
    rationale = output.get("rationale") if isinstance(output, dict) else None
    proposal.status = "suggested" if proposed else "no_change"
    proposal.proposed = proposed
    proposal.rationale = rationale.strip() if isinstance(rationale, str) else ""
    proposal.tokens = int(input_tokens) + int(output_tokens)
    proposal.ai_metadata = {**metadata, "input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_proposal(p: DriftProposal) -> dict:
    return {
        "id": p.id,
        "code_id": p.code_id,
        "status": p.status,
        "base": p.base,
        "proposed": p.proposed,
        "applied": p.applied,
        "rationale": p.rationale,
        "disagreements": p.disagreements,
        "session_judgments": p.session_judgments,
        "ai_metadata": p.ai_metadata,
        "tokens": p.tokens,
        "score_status": p.score_status,
        "score_summary": p.score_summary,
        "created_at": _iso(p.created_at),
        "decided_at": _iso(p.decided_at),
    }


def proposals_for(db: Session, code_id: int) -> list[DriftProposal]:
    """Newest first."""
    if db.get(Code, code_id) is None:
        raise CodeNotFound(code_id)
    return list(
        db.scalars(select(DriftProposal).where(DriftProposal.code_id == code_id).order_by(DriftProposal.id.desc()))
    )
