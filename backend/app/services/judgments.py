from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import CorticoSession
from ..models import Code, Coding, HighlightStatus, RunHighlight, SearchRun
from ..models_cortico import Conversation, Highlight
from . import embeddings
from .codebook_edit import CodeNotFound

Status = Literal["not_started", "in_progress", "completed"]

# Cortico's bulkMarkAsNotDone sets "Pending", the undo of bulkMarkAsDone's "Done"; neither
# is in its highlight status menu, so "not done" maps to the start of this app's workflow.
NOT_DONE: Status = "not_started"


class UnknownHighlights(LookupError):
    def __init__(self, ids: list[int]) -> None:
        super().__init__(f"Highlights {ids} are not in the snapshot.")
        self.ids = ids


@dataclass
class Latest:
    """The newest human and AI rows of one code on one highlight. Rows are append-only, so
    newest is the highest id (created_at can tie)."""

    human: str | None = None
    human_id: int = 0
    ai_probability: float | None = None
    ai_id: int = 0

    def final(self, threshold: float) -> bool | None:
        if self.human is not None:
            return self.human == "applied"
        if self.ai_probability is not None:
            return self.ai_probability >= threshold
        return None


def _conversations(db_c: Session, highlight_ids: Iterable[int]) -> dict[int, int]:
    """highlight_id -> conversation_id; raises UnknownHighlights if any id is missing."""
    wanted = set(highlight_ids)
    found = dict(db_c.execute(select(Highlight.id, Highlight.conversation_id).where(Highlight.id.in_(wanted))).all())
    missing = sorted(wanted - found.keys())
    if missing:
        raise UnknownHighlights(missing)
    return found


def _with_cortico(db_c: Session | None, fn):
    if db_c is not None:
        return fn(db_c)
    with CorticoSession() as session:
        return fn(session)


def judge(
    db: Session, code_id: int, highlight_ids: list[int], applied: bool, db_c: Session | None = None
) -> int:
    """Appends one human row per highlight; every id is checked before anything is written."""
    if db.get(Code, code_id) is None:
        raise CodeNotFound(code_id)
    conversations = _with_cortico(db_c, lambda s: _conversations(s, highlight_ids))
    user_id = get_config().user_id
    status = "applied" if applied else "not_applied"
    for hid in highlight_ids:
        db.add(
            Coding(
                code_id=code_id, highlight_id=hid, conversation_id=conversations[hid],
                highlight_share_id=None, is_ai_generated=False, ai_probability=None,
                status=status, created_by=user_id,
            )
        )
    db.commit()
    return len(highlight_ids)


def set_status(db: Session, highlight_ids: list[int], status: Status, db_c: Session | None = None) -> None:
    """Upserts one highlight_status row per highlight; rows are never deleted."""
    _with_cortico(db_c, lambda s: _conversations(s, highlight_ids))
    existing = {
        row.highlight_id: row
        for row in db.scalars(select(HighlightStatus).where(HighlightStatus.highlight_id.in_(set(highlight_ids))))
    }
    for hid in dict.fromkeys(highlight_ids):
        if hid in existing:
            existing[hid].status = status
        else:
            db.add(HighlightStatus(highlight_id=hid, status=status))
    db.commit()


def latest_by_pair(
    db: Session, code_ids: Iterable[int] | None = None, highlight_ids: Iterable[int] | None = None
) -> dict[tuple[int, int], Latest]:
    """(code_id, highlight_id) -> Latest, loaded in one query."""
    query = select(Coding.id, Coding.code_id, Coding.highlight_id, Coding.is_ai_generated, Coding.status, Coding.ai_probability)
    if code_ids is not None:
        query = query.where(Coding.code_id.in_(set(code_ids)))
    if highlight_ids is not None:
        query = query.where(Coding.highlight_id.in_(set(highlight_ids)))
    out: dict[tuple[int, int], Latest] = {}
    for row_id, code_id, hid, is_ai, status, p in db.execute(query):
        latest = out.setdefault((code_id, hid), Latest())
        if is_ai:
            if row_id > latest.ai_id:
                latest.ai_id, latest.ai_probability = row_id, p
        elif row_id > latest.human_id:
            latest.human_id, latest.human = row_id, status
    return out


def final_for(db: Session, code_id: int, highlight_id: int) -> bool | None:
    code = db.get(Code, code_id)
    if code is None:
        raise CodeNotFound(code_id)
    latest = latest_by_pair(db, [code_id], [highlight_id]).get((code_id, highlight_id))
    return latest.final(code.threshold) if latest else None


def _human_ai_pairs(db: Session, code_id: int) -> list[tuple[int, bool, bool]]:
    """(highlight_id, human yes, AI yes) for highlights with both sides, by highlight id."""
    code = db.get(Code, code_id)
    if code is None:
        raise CodeNotFound(code_id)
    out = []
    for (_, highlight_id), latest in latest_by_pair(db, [code_id]).items():
        if latest.human is None or latest.ai_probability is None:
            continue
        out.append((highlight_id, latest.human == "applied", latest.ai_probability >= code.threshold))
    return sorted(out)


def agreement_stats(pairs: Iterable[tuple[bool, bool]]) -> dict:
    """Agreement counts, percent match and Cohen's kappa over (human, AI) yes/no pairs."""
    counts = {"both_yes": 0, "both_no": 0, "human_only": 0, "ai_only": 0}
    for human, ai in pairs:
        key = "both_yes" if human and ai else "both_no" if not human and not ai else "human_only" if human else "ai_only"
        counts[key] += 1
    n = sum(counts.values())
    percent_match = kappa = None
    if n:
        percent_match = (counts["both_yes"] + counts["both_no"]) / n
        human_yes = (counts["both_yes"] + counts["human_only"]) / n
        ai_yes = (counts["both_yes"] + counts["ai_only"]) / n
        expected = human_yes * ai_yes + (1 - human_yes) * (1 - ai_yes)
        if expected < 1:
            kappa = (percent_match - expected) / (1 - expected)
    return {"n": n, **counts, "percent_match": percent_match, "kappa": kappa}


def agreement(db: Session, code_id: int) -> dict:
    """Human vs AI over highlights that have both, using the latest row of each; AI yes is a
    probability at or above the code's current threshold."""
    return agreement_stats((human, ai) for _, human, ai in _human_ai_pairs(db, code_id))


def disagreements(db: Session, code_id: int) -> list[int]:
    """Highlight ids, ascending, whose latest human call and latest AI call differ."""
    return [hid for hid, human, ai in _human_ai_pairs(db, code_id) if human != ai]


def current_samples(db: Session) -> dict[int, tuple[int, dict]]:
    """code_id -> (run_id, that run's summary entry for the code) for the code's current
    sample: the newest sample run (summary kind "sample") that is not running and has at least
    one answered highlight for the code. Older all-codes runs have no kind and are not samples."""
    out: dict[int, tuple[int, dict]] = {}
    runs = db.execute(
        select(SearchRun.id, SearchRun.summary)
        .where(SearchRun.scope_code_id.is_(None), SearchRun.status != "running")
        .order_by(SearchRun.id.desc())
    )
    for run_id, summary in runs:
        summary = summary or {}
        if summary.get("kind") != "sample":
            continue
        for key, progress in (summary.get("codes") or {}).items():
            if progress.get("tries", 0) > 0:
                out.setdefault(int(key), (run_id, progress))
    return out


def latest_sample_run_id(db: Session, code_id: int) -> int | None:
    """The run id of the code's current sample (see current_samples), or None."""
    current = current_samples(db).get(code_id)
    return current[0] if current else None


def review(db: Session, db_c: Session, code_id: int) -> dict:
    """The code's current sample and every find-more run for the code, one row per highlight, with
    priority found > random > below threshold. Find-more rows include older runs' per-code
    searches (run_highlight rows targeting this code)."""
    code = db.get(Code, code_id)
    if code is None:
        raise CodeNotFound(code_id)

    sample_run_id = latest_sample_run_id(db, code_id)
    # In judging order.
    sample_ids = (
        list(
            dict.fromkeys(
                db.scalars(
                    select(Coding.highlight_id)
                    .where(
                        Coding.run_id == sample_run_id, Coding.code_id == code_id,
                        Coding.is_ai_generated.is_(True),
                    )
                    .order_by(Coding.id)
                )
            )
        )
        if sample_run_id is not None
        else []
    )
    # Newest run first, then run order.
    search_rows = list(
        db.execute(
            select(RunHighlight.run_id, RunHighlight.highlight_id, RunHighlight.picked_by)
            .where(RunHighlight.target_code_id == code_id)
            .order_by(RunHighlight.run_id.desc(), RunHighlight.position, RunHighlight.id)
        )
    )
    find_more_run_ids = list(dict.fromkeys(run_id for run_id, _, _ in search_rows))

    # Sample wins, then a random pick in any find-more run, so labels match the sections.
    picked_random = {hid for _, hid, by in search_rows if by == "random"}
    source: dict[int, str] = {h: "sample" for h in sample_ids}
    for _, hid, _ in search_rows:
        source.setdefault(hid, "random" if hid in picked_random else "search")
    candidates = list(source)

    this_code = latest_by_pair(db, [code_id])

    def is_found(h: int) -> bool:
        latest = this_code.get((code_id, h), Latest())
        ai_yes = latest.ai_probability is not None and latest.ai_probability >= code.threshold
        return ai_yes or latest.final(code.threshold) is True

    found_listed = [h for h in candidates if is_found(h)]
    found_set = set(found_listed)
    random_ids = [h for h in candidates if h not in found_set and source[h] in ("sample", "random")]
    random_set = set(random_ids)
    below_ids = [h for h in candidates if h not in found_set and h not in random_set]
    final_yes = [h for (_, h), latest in this_code.items() if latest.final(code.threshold) is True]
    extra = [h for h in final_yes if h not in source]
    # Final-yes highlights from no current source: newest judgment first.
    extra.sort(key=lambda h: -max(this_code[(code_id, h)].human_id, this_code[(code_id, h)].ai_id))
    found_ids = found_listed + extra

    listed = list(dict.fromkeys(found_ids + below_ids + random_ids))
    others = [c for c in embeddings.current_active_codes(db) if c.id != code_id]
    other_latest = latest_by_pair(db, [c.id for c in others], listed) if others and listed else {}
    texts = {
        hid: (text, conversation_id)
        for hid, text, conversation_id in db_c.execute(
            select(Highlight.id, Highlight.text, Highlight.conversation_id).where(Highlight.id.in_(listed))
        )
    }
    projects = dict(
        db_c.execute(
            select(Conversation.id, Conversation.project_id).where(
                Conversation.id.in_({c for _, c in texts.values()})
            )
        ).all()
    )
    statuses = dict(
        db.execute(select(HighlightStatus.highlight_id, HighlightStatus.status).where(HighlightStatus.highlight_id.in_(listed))).all()
    )

    def row(hid: int) -> dict:
        text, conversation_id = texts.get(hid, ("", None))
        latest = this_code.get((code_id, hid), Latest())
        return {
            "highlight_id": hid,
            "conversation_id": conversation_id,
            "project_id": projects.get(conversation_id),
            "text": text,
            "ai_probability": latest.ai_probability,
            "human": latest.human,
            "final": latest.final(code.threshold),
            "status": statuses.get(hid, "not_started"),
            "source": source.get(hid),
            "other_codes": [
                {
                    "code_id": c.id,
                    "name": c.name,
                    "final": other_latest[(c.id, hid)].final(c.threshold) if (c.id, hid) in other_latest else None,
                }
                for c in others
            ],
        }

    return {
        "code": {"id": code.id, "name": code.name, "definition": code.definition, "threshold": code.threshold, "status": code.status},
        "sample_run_id": sample_run_id,
        "find_more_run_ids": find_more_run_ids,
        "agreement": agreement(db, code_id),
        "found": [row(h) for h in found_ids],
        "below_threshold": [row(h) for h in below_ids],
        "random": [row(h) for h in random_ids],
    }
