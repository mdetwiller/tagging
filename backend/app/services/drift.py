from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import CorticoSession, TaggingSession
from ..models import Code, CodeRevision, DriftProposal, ProposalScore
from ..models_cortico import Highlight
from ..schemas import CodeEdit
from . import judgments, rules, search_run
from .codebook_edit import CodeNotFound, edit_code
from .export import _iso
from .jev import HttpJevClient, JevClient, JevError, build_keyed_request, parse_keyed_noul

log = logging.getLogger(__name__)

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

        self._client = OpenAI(
            api_key=api_key, base_url=config.openai_base_url() or None, timeout=config.drift.timeout_seconds
        )
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


def stale_parts(db: Session, p: DriftProposal) -> list[str]:
    """For a suggested proposal, the parts whose live value no longer matches `base` (accepting
    them would be refused). Empty for any other status."""
    if p.status != "suggested" or not p.base:
        return []
    code = db.get(Code, p.code_id)
    if code is None:
        return []
    current = _base(db, code)
    return [
        part for part, fields in PART_FIELDS.items() if any(current[f] != p.base.get(f) for f in fields)
    ]


def serialize_proposal(db: Session, p: DriftProposal) -> dict:
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
        "stale_parts": stale_parts(db, p),
    }


def proposals_for(db: Session, code_id: int) -> list[DriftProposal]:
    """Newest first."""
    if db.get(Code, code_id) is None:
        raise CodeNotFound(code_id)
    return list(
        db.scalars(select(DriftProposal).where(DriftProposal.code_id == code_id).order_by(DriftProposal.id.desc()))
    )


# "Test with Jev": re-score a proposal against the code's human-judged highlights.

CUR, NEW = "cur", "new"
_score_threads: dict[int, threading.Thread] = {}


class ProposalNotFound(LookupError):
    pass


class ScoreConflict(RuntimeError):
    pass


def get_proposal(db: Session, proposal_id: int) -> DriftProposal:
    proposal = db.get(DriftProposal, proposal_id)
    if proposal is None:
        raise ProposalNotFound(f"Proposal {proposal_id} does not exist.")
    return proposal


def _judged(db: Session, code_id: int) -> dict[int, str]:
    """highlight_id -> latest human verdict, for highlights whose latest human row for the code
    is applied or not_applied."""
    return {
        hid: latest.human
        for (_, hid), latest in judgments.latest_by_pair(db, [code_id]).items()
        if latest.human in ("applied", "not_applied")
    }


class _Scoring:
    """Running totals of one score run; the summary is built only from this run's answers."""

    def __init__(self, current_threshold: float, proposed_threshold: float) -> None:
        self.current_threshold, self.proposed_threshold = current_threshold, proposed_threshold
        self.before: list[tuple[bool, bool]] = []
        self.after: list[tuple[bool, bool]] = []
        self.missing_answers = 0
        self.failed_requests = 0

    def add(self, human: str, current_p: float | None, proposed_p: float | None) -> None:
        yes = human == "applied"
        if current_p is not None:
            self.before.append((yes, current_p >= self.current_threshold))
        if proposed_p is not None:
            self.after.append((yes, proposed_p >= self.proposed_threshold))

    def summary(self) -> dict:
        return {
            "before": judgments.agreement_stats(self.before),
            "after": judgments.agreement_stats(self.after),
            "missing_answers": self.missing_answers,
            "failed_requests": self.failed_requests,
        }


def score_proposal(db_c: Session, db_t: Session, proposal_id: int, client: JevClient) -> None:
    """Asks Jev, for each highlight judged by a human for the proposal's code, the current
    question (`cur`) and the proposed one (`new`), with the highlight left out of both
    questions' examples. When the two texts are the same (a threshold-only proposal) only `cur`
    is asked, and its probability is scored against both thresholds. Appends proposal_score rows and replaces score_summary; writes nothing
    to `coding`. Failure rules match search_run._Judging. On any error the score is marked
    failed, rows already committed stay, and the error is re-raised."""
    scoring: _Scoring | None = None
    try:
        proposal = get_proposal(db_t, proposal_id)
        code = db_t.get(Code, proposal.code_id)
        proposed = dict(proposal.proposed or {})
        scoring = _Scoring(code.threshold, float(proposed.get("threshold", code.threshold)))
        proposal.score_status, proposal.score_summary = "running", scoring.summary()
        db_t.commit()

        judged = _judged(db_t, code.id)
        texts = dict(
            db_c.execute(select(Highlight.id, Highlight.text).where(Highlight.id.in_(list(judged)))).all()
        ) if judged else {}
        hids = sorted(h for h in judged if h in texts)
        config = get_config()
        size = max(1, config.search.jev_concurrency)
        failed_batches = 0

        def body(hid: int) -> tuple[dict, list[str]]:
            """The request and its question keys. When the proposal changes only the threshold
            the two questions are the same text, so only `cur` is asked and serves both."""
            cur = search_run.code_instructions(db_t, code, texts, exclude_highlight=hid)
            new = search_run.code_instructions(db_t, code, texts, exclude_highlight=hid, override=proposed)
            questions = [(CUR, cur)] if cur == new else [(CUR, cur), (NEW, new)]
            keys = [k for k, _ in questions]
            return build_keyed_request({"id": hid, "text": texts[hid]}, questions, config.jev.model), keys

        with ThreadPoolExecutor(max_workers=size) as pool:
            for start in range(0, len(hids), size):
                batch = hids[start : start + size]
                requests = [body(hid) for hid in batch]
                futures = [pool.submit(client.judge, request) for request, _ in requests]
                error: JevError | None = None
                batch_failures = 0
                for hid, (_, keys), future in zip(batch, requests, futures):
                    try:
                        response = future.result()
                    except JevError as exc:
                        if exc.status in search_run.FATAL_JEV_STATUSES:
                            raise
                        log.warning("Proposal %s score: Jev request for highlight %s failed: %s", proposal_id, hid, exc)
                        error, batch_failures = exc, batch_failures + 1
                        scoring.failed_requests += 1
                        continue
                    answers, missing = parse_keyed_noul(response, keys)
                    scoring.missing_answers += len(missing)
                    current_p = answers.get(CUR)
                    proposed_p = answers.get(NEW) if NEW in keys else current_p
                    db_t.add(
                        ProposalScore(
                            proposal_id=proposal_id, highlight_id=hid, human=judged[hid],
                            current_p=current_p, proposed_p=proposed_p,
                        )
                    )
                    scoring.add(judged[hid], current_p, proposed_p)
                    proposal.score_summary = scoring.summary()
                    db_t.commit()
                if batch_failures:
                    proposal.score_summary = scoring.summary()
                    db_t.commit()
                failed_batches = failed_batches + 1 if batch_failures == len(batch) else 0
                if failed_batches >= search_run.MAX_CONSECUTIVE_FAILED_BATCHES:
                    raise error

        proposal.score_status, proposal.score_summary = "done", scoring.summary()
        db_t.commit()
    except Exception as exc:
        db_t.rollback()
        proposal = db_t.get(DriftProposal, proposal_id)
        if proposal is not None:
            proposal.score_status = "failed"
            proposal.score_summary = {
                **(scoring.summary() if scoring else {}), "error": f"{type(exc).__name__}: {exc}",
            }
            db_t.commit()
        raise


def start_score(proposal_id: int) -> None:
    """Marks a suggested proposal's score running and runs it on a daemon thread with its own
    sessions. Raises ProposalNotFound, or ScoreConflict when the proposal is not suggested, its
    code is not active, or any Jev job (search run or score) is running."""
    with search_run._start_lock:
        with TaggingSession() as db_t:
            proposal = get_proposal(db_t, proposal_id)
            if proposal.status != "suggested":
                raise ScoreConflict(f"Proposal {proposal_id} is {proposal.status}; only a suggested proposal can be scored.")
            code = db_t.get(Code, proposal.code_id)
            if code is None or code.status != "ACTIVE":
                status = code.status.lower() if code is not None else "missing"
                raise ScoreConflict(f"Code {proposal.code_id} is {status}; a proposal for it cannot be scored.")
            try:
                search_run.check_no_jev_job(db_t)
            except search_run.RunConflict as exc:
                raise ScoreConflict(str(exc)) from exc
            proposal.score_status, proposal.score_summary = "running", None
            db_t.commit()
        thread = threading.Thread(
            target=_score_background, args=(proposal_id,), daemon=True, name=f"proposal-score-{proposal_id}"
        )
        _score_threads[proposal_id] = thread
        thread.start()


def _score_background(proposal_id: int) -> None:
    error = "The score stopped without finishing."
    try:
        with CorticoSession() as db_c, TaggingSession() as db_t:
            client = HttpJevClient()
            score_proposal(db_c, db_t, proposal_id, client)
    except Exception as exc:
        log.exception("Score of proposal %s failed", proposal_id)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # A score left running would refuse every Jev job until a restart.
        _fail_if_running(proposal_id, error)


def _fail_if_running(proposal_id: int, error: str) -> None:
    try:
        with TaggingSession() as db_t:
            proposal = db_t.get(DriftProposal, proposal_id)
            if proposal is not None and proposal.score_status == "running":
                proposal.score_status = "failed"
                proposal.score_summary = {**(proposal.score_summary or {}), "error": error}
                db_t.commit()
    except Exception:
        log.exception("Could not mark the score of proposal %s failed", proposal_id)


def join_score(proposal_id: int, timeout: float | None = None) -> None:
    thread = _score_threads.get(proposal_id)
    if thread is not None:
        thread.join(timeout)


def fail_orphaned_scores(db_t: Session) -> None:
    """Scores left `running` by a stopped server have no thread; mark them failed."""
    for proposal in db_t.scalars(select(DriftProposal).where(DriftProposal.score_status == "running")):
        thread = _score_threads.get(proposal.id)
        if thread is not None and thread.is_alive():
            continue
        proposal.score_status = "failed"
        proposal.score_summary = {**(proposal.score_summary or {}), "error": "The server stopped during the score."}
    db_t.commit()


# Deciding on a proposal: accept (optionally edited), dismiss, roll back.

CODE_FIELDS = ("name", "definition", "threshold")
RULE_FIELDS = ("include", "exclude")
PART_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("name",), "definition": ("definition",), "threshold": ("threshold",), "rules": RULE_FIELDS,
}


class DecisionConflict(RuntimeError):
    """The proposal or its code is not in a state that allows the decision (409)."""


class DecisionInvalid(ValueError):
    """The request itself is unusable (400)."""


def _taken_names(db: Session, code: Code) -> set[str]:
    return {
        n.strip().casefold()
        for n in db.scalars(
            select(Code.name).where(Code.codebook_id == code.codebook_id, Code.status == "ACTIVE", Code.id != code.id)
        )
    }


def _check_values(db: Session, code: Code, final: dict) -> dict:
    """`final` with strings trimmed, or DecisionInvalid."""
    checked = dict(final)
    if "name" in final:
        name = final["name"].strip() if isinstance(final["name"], str) else ""
        if not name:
            raise DecisionInvalid("A code name cannot be blank.")
        if name.casefold() in _taken_names(db, code):
            raise DecisionInvalid(f"Another active code is already named {name!r}.")
        checked["name"] = name
    if "definition" in final:
        definition = final["definition"].strip() if isinstance(final["definition"], str) else ""
        if not definition:
            raise DecisionInvalid("A definition cannot be blank.")
        checked["definition"] = definition
    if "threshold" in final:
        threshold = final["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise DecisionInvalid("The threshold must be between 0 and 1.")
        checked["threshold"] = float(threshold)
    for field in RULE_FIELDS:
        if field not in final:
            continue
        items = final[field]
        if not isinstance(items, list) or not all(isinstance(s, str) and s.strip() for s in items):
            raise DecisionInvalid(f"Each {field} rule must be non-empty text.")
        if len(items) > MAX_RULES:
            raise DecisionInvalid(f"At most {MAX_RULES} {field} rules.")
        checked[field] = [s.strip() for s in items]
    return checked


def _apply(db: Session, code_id: int, values: dict, rules_source: str, proposal_id: int) -> list[int]:
    """Writes the code fields through edit_code (one code_revision per changed field) and, when
    `values` has rules, a new code_rules row. Returns the new code_revision ids."""
    before = db.scalar(select(func.max(CodeRevision.id)).where(CodeRevision.code_id == code_id)) or 0
    changes = {f: values[f] for f in CODE_FIELDS if f in values}
    if changes:
        edit_code(db, code_id, CodeEdit(**changes))
    if "include" in values and (values["include"], values["exclude"]) != rules.current_rules(db, code_id):
        rules.set_rules(db, code_id, values["include"], values["exclude"], source=rules_source, proposal_id=proposal_id)
    return list(
        db.scalars(
            select(CodeRevision.id)
            .where(CodeRevision.code_id == code_id, CodeRevision.id > before)
            .order_by(CodeRevision.id)
        )
    )


def accept(db: Session, proposal_id: int, parts: list[str], values: dict | None = None) -> DriftProposal:
    """Applies the accepted parts of a suggested proposal, with the researcher's `values`
    winning over `proposed`. Everything is checked before anything is written."""
    with search_run._start_lock:  # a score cannot start on this proposal mid-accept
        proposal = get_proposal(db, proposal_id)
        code = db.get(Code, proposal.code_id)
        if code.status != "ACTIVE":
            raise DecisionConflict(f"Code {code.id} is {code.status.lower()}; a proposal for it cannot be accepted.")
        if proposal.status != "suggested":
            raise DecisionConflict(f"Proposal {proposal_id} is {proposal.status}; only a suggested proposal can be accepted.")
        if proposal.score_status == "running":
            raise DecisionConflict(f"Proposal {proposal_id} is being tested with Jev; accept it when the test finishes.")
        parts = list(dict.fromkeys(parts))
        if not parts:
            raise DecisionInvalid("Choose at least one part to accept.")
        values = {k: v for k, v in (values or {}).items() if v is not None}
        accepted_fields = {f for p in parts for f in PART_FIELDS[p]}
        stray = sorted(set(values) - accepted_fields)
        if stray:
            raise DecisionInvalid(f"Values were given for parts not accepted: {', '.join(stray)}.")
        proposed = dict(proposal.proposed or {})
        for part in parts:
            if not any(f in values or f in proposed for f in PART_FIELDS[part]):
                raise DecisionInvalid(f"Proposal {proposal_id} has no {part} change to accept.")

        current = _base(db, code)
        final = {
            f: values[f] if f in values else proposed[f] if f in proposed else current[f]
            for f in CODE_FIELDS + RULE_FIELDS
            if f in accepted_fields
        }
        final = _check_values(db, code, final)
        for part in parts:
            if any(current[f] != proposal.base[f] for f in PART_FIELDS[part]):
                raise DecisionConflict(
                    f"The code's {part} changed after proposal {proposal_id} was drafted; "
                    f"field '{part}' no longer matches the proposal's starting point."
                )
        edited = any(final[f] != proposed.get(f, current[f]) for f in values)

        revision_ids = _apply(db, code.id, final, "proposal", proposal.id)
        proposal.status = "accepted"
        proposal.applied = {**final, "edited": edited, "revision_ids": revision_ids}
        proposal.decided_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(proposal)
        return proposal


def dismiss(db: Session, proposal_id: int) -> DriftProposal:
    with search_run._start_lock:  # a score cannot start on this proposal mid-dismiss
        proposal = get_proposal(db, proposal_id)
        if proposal.status != "suggested":
            raise DecisionConflict(f"Proposal {proposal_id} is {proposal.status}; only a suggested proposal can be dismissed.")
        if proposal.score_status == "running":
            raise DecisionConflict(f"Proposal {proposal_id} is being tested with Jev; dismiss it when the test finishes.")
        proposal.status = "dismissed"
        proposal.decided_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(proposal)
        return proposal


def rollback(db: Session, proposal_id: int) -> DriftProposal:
    """Restores the `base` values of the code's newest accepted proposal, provided nothing
    the proposal applied has been changed since."""
    with search_run._start_lock:  # a score of this code cannot start mid-rollback
        proposal = get_proposal(db, proposal_id)
        newest = db.scalars(
            select(DriftProposal)
            .where(DriftProposal.code_id == proposal.code_id, DriftProposal.status == "accepted")
            .order_by(DriftProposal.id.desc())
            .limit(1)
        ).first()
        if newest is None or newest.id != proposal.id:
            raise DecisionConflict(
                f"Proposal {proposal_id} is {proposal.status}; only the code's newest accepted proposal can be rolled back."
            )
        running = db.scalars(
            select(DriftProposal.id).where(
                DriftProposal.code_id == proposal.code_id, DriftProposal.score_status == "running"
            )
        ).first()
        if running is not None:
            raise DecisionConflict(
                f"Proposal {running} for this code is being tested with Jev; roll back when the test finishes."
            )
        applied = dict(proposal.applied or {})
        code = db.get(Code, proposal.code_id)
        current = _base(db, code)
        restore: dict = {}
        for part, fields in PART_FIELDS.items():
            if not all(f in applied for f in fields):
                continue
            if any(current[f] != applied[f] for f in fields):
                raise DecisionConflict(
                    f"The code's {part} was changed after proposal {proposal_id} was accepted; "
                    f"field '{part}' no longer matches what the proposal applied."
                )
            restore.update({f: proposal.base[f] for f in fields})
        if "name" in restore and restore["name"].strip().casefold() in _taken_names(db, code):
            raise DecisionConflict(f"Another active code is now named {restore['name']!r}; field 'name' cannot be restored.")

        revision_ids = _apply(db, code.id, restore, "rollback", proposal.id)
        proposal.status = "rolled_back"
        proposal.applied = {**applied, "rollback_revision_ids": revision_ids}
        proposal.decided_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(proposal)
        return proposal
