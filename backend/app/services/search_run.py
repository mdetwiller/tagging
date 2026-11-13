from __future__ import annotations

import hashlib
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..db import CorticoSession, TaggingSession
from ..models import Code, CodeExemplar, Coding, RunHighlight, SearchRun
from ..models_cortico import Highlight
from . import embeddings
from .embeddings import rank_candidates
from .jev import MAX_HUMAN_EXAMPLES, HttpJevClient, JevClient, build_instructions, build_request, parse_noul

log = logging.getLogger(__name__)

Rank = Callable[[int], list[int]]


class RunConflict(RuntimeError):
    pass


class ScopeCodeNotFound(LookupError):
    pass


@dataclass(frozen=True)
class _CodeSnapshot:
    id: int
    threshold: float
    instructions: str
    instructions_hash: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _human_examples(db_t: Session, code_id: int, texts: dict[int, str]) -> tuple[list[str], list[str]]:
    """Texts of the newest highlights whose latest human coding for the code is applied or
    not_applied, newest first."""
    rows = db_t.execute(
        select(Coding.highlight_id, Coding.status)
        .where(Coding.code_id == code_id, Coding.is_ai_generated.is_(False))
        .order_by(Coding.id.desc())
    ).all()
    latest: dict[int, str] = {}
    for hid, status in rows:
        latest.setdefault(hid, status)
    accepted = [texts[h] for h, s in latest.items() if s == "applied" and h in texts]
    rejected = [texts[h] for h, s in latest.items() if s == "not_applied" and h in texts]
    return accepted[:MAX_HUMAN_EXAMPLES], rejected[:MAX_HUMAN_EXAMPLES]


def code_instructions(db_t: Session, code: Code, texts: dict[int, str]) -> str:
    """The Noul instructions Jev sees for the code now. `texts` maps highlight_id to text and
    must cover the code's human-judged highlights."""
    exemplars = list(
        db_t.scalars(select(CodeExemplar).where(CodeExemplar.code_id == code.id).order_by(CodeExemplar.id))
    )
    accepted, rejected = _human_examples(db_t, code.id, texts)
    return build_instructions(code, exemplars, accepted, rejected)


def _snapshot(db_t: Session, code: Code, texts: dict[int, str]) -> _CodeSnapshot:
    instructions = code_instructions(db_t, code, texts)
    return _CodeSnapshot(
        id=code.id,
        threshold=code.threshold,
        instructions=instructions,
        instructions_hash=hashlib.sha256(instructions.encode()).hexdigest(),
    )


def _human_judged(db_t: Session, code_ids: list[int]) -> dict[int, set[int]]:
    """Highlights with any human coding, per code."""
    judged: dict[int, set[int]] = {c: set() for c in code_ids}
    for code_id, hid in db_t.execute(
        select(Coding.code_id, Coding.highlight_id).where(
            Coding.is_ai_generated.is_(False), Coding.code_id.in_(code_ids)
        )
    ):
        judged[code_id].add(hid)
    return judged


def _queue(
    ranked: list[int],
    pool: list[int],
    excluded: set[int],
    random_every: int,
    rng: random.Random,
) -> Iterator[tuple[int, str, int]]:
    """(highlight_id, picked_by, position), 1-based. Every `random_every`-th slot is a random
    highlight; when one source runs out the other fills its slots."""
    shuffled = list(pool)
    rng.shuffle(shuffled)
    sources = {"similarity": iter(ranked), "random": iter(shuffled)}
    used = set(excluded)

    def take(kind: str) -> int | None:
        for hid in sources[kind]:
            if hid not in used:
                used.add(hid)
                return hid
        return None

    position = 0
    while True:
        position += 1
        first = "random" if random_every and position % random_every == 0 else "similarity"
        second = "similarity" if first == "random" else "random"
        for kind in (first, second):
            hid = take(kind)
            if hid is not None:
                yield hid, kind, position
                break
        else:
            return


def create_run(db_t: Session, scope_code_id: int | None) -> int:
    run = SearchRun(scope_code_id=scope_code_id, status="running", requests=0, tokens_used=0)
    db_t.add(run)
    db_t.commit()
    return run.id


def run_search(
    db_c: Session,
    db_t: Session,
    run_id: int,
    client: JevClient,
    rng: random.Random,
    rank: Rank = rank_candidates,
) -> None:
    """Runs the search for an existing `running` SearchRun. On any error the run is marked
    failed, rows already committed stay, and the error is re-raised."""
    try:
        _search(db_c, db_t, run_id, client, rng, rank)
    except Exception as exc:
        db_t.rollback()
        run = db_t.get(SearchRun, run_id)
        run.status = "failed"
        run.finished_at = _now()
        run.summary = {**(run.summary or {}), "error": f"{type(exc).__name__}: {exc}"}
        db_t.commit()
        raise


def _search(
    db_c: Session, db_t: Session, run_id: int, client: JevClient, rng: random.Random, rank: Rank
) -> None:
    config = get_config()
    cfg, model, user_id = config.search, config.jev.model, config.user_id
    run = db_t.get(SearchRun, run_id)
    scope_code_id = run.scope_code_id

    highlights = {
        hid: (text, conversation_id)
        for hid, text, conversation_id in db_c.execute(
            select(Highlight.id, Highlight.text, Highlight.conversation_id).order_by(Highlight.id)
        )
    }
    texts = {hid: text for hid, (text, _) in highlights.items()}
    all_ids = list(highlights)

    # Snapshotted once; codes retired mid-run keep being judged and written under these ids.
    judge = [_snapshot(db_t, c, texts) for c in embeddings.current_active_codes(db_t)]
    by_id = {s.id: s for s in judge}
    targets = [scope_code_id] if scope_code_id is not None else [s.id for s in judge]
    questions = [(s.id, s.instructions) for s in judge]
    judge_ids = [s.id for s in judge]
    human_judged = _human_judged(db_t, sorted(set(judge_ids) | set(targets)))

    positives = {c: 0 for c in judge_ids}
    tries = {c: 0 for c in targets}
    missing_answers = 0
    requests = 0
    tokens = 0
    judged: set[int] = set()
    random_every = round(1 / cfg.random_share) if cfg.random_share > 0 else 0

    def summary() -> dict:
        return {
            "codes": {
                str(c): {
                    "positives": positives[c],
                    "tries": tries[c],
                    "hard_to_find": positives[c] < cfg.hard_to_find_below,
                }
                for c in targets
            },
            "missing_answers": missing_answers,
        }

    remaining = set(targets)
    with ThreadPoolExecutor(max_workers=max(1, cfg.jev_concurrency)) as pool:
        while remaining:
            code = min(remaining, key=lambda c: (positives[c], c))
            remaining.remove(code)
            excluded = human_judged[code] | judged
            queue = _queue(rank(code), all_ids, excluded, random_every, rng)
            while positives[code] < cfg.target_positives and tries[code] < cfg.max_tries_per_code:
                size = min(cfg.jev_concurrency, cfg.max_tries_per_code - tries[code])
                batch = [item for _, item in zip(range(size), queue)]
                if not batch:
                    break
                for hid, picked_by, position in batch:
                    tries[code] += 1
                    judged.add(hid)
                    db_t.add(
                        RunHighlight(
                            run_id=run_id, target_code_id=code, highlight_id=hid,
                            picked_by=picked_by, position=position,
                        )
                    )
                db_t.commit()

                futures = [
                    pool.submit(
                        client.judge,
                        build_request({"id": hid, "text": highlights[hid][0]}, questions, model),
                    )
                    for hid, _, _ in batch
                ]
                error: BaseException | None = None
                for (hid, _, _), future in zip(batch, futures):
                    try:
                        response = future.result()
                    except Exception as exc:
                        error = error or exc
                        continue
                    requests += 1
                    tokens += int((response.get("usage") or {}).get("input_tokens") or 0)
                    probabilities, missing = parse_noul(response, judge_ids)
                    missing_answers += len(missing)
                    for code_id, p in probabilities.items():
                        snap = by_id[code_id]
                        db_t.add(
                            Coding(
                                code_id=code_id, highlight_id=hid,
                                conversation_id=highlights[hid][1],
                                is_ai_generated=True, ai_probability=p, status="suggested",
                                created_by=user_id, run_id=run_id,
                                ai_metadata={
                                    "model": model,
                                    "run_id": run_id,
                                    "question_instructions_hash": snap.instructions_hash,
                                },
                            )
                        )
                        if p >= snap.threshold and hid not in human_judged[code_id]:
                            positives[code_id] += 1
                    run = db_t.get(SearchRun, run_id)
                    run.requests, run.tokens_used, run.summary = requests, tokens, summary()
                    db_t.commit()
                if error is not None:
                    raise error

    run = db_t.get(SearchRun, run_id)
    run.status = "done"
    run.finished_at = _now()
    run.requests, run.tokens_used, run.summary = requests, tokens, summary()
    db_t.commit()


_start_lock = threading.Lock()
_threads: dict[int, threading.Thread] = {}


def _check_can_start(db_c: Session, db_t: Session, scope_code_id: int | None) -> None:
    if db_t.scalars(select(SearchRun.id).where(SearchRun.status == "running")).first() is not None:
        raise RunConflict("A search run is already running.")
    active = {c.id for c in embeddings.current_active_codes(db_t)}
    if scope_code_id is not None:
        if db_t.get(Code, scope_code_id) is None:
            raise ScopeCodeNotFound(f"Code {scope_code_id} does not exist.")
        if scope_code_id not in active:
            raise RunConflict(f"Code {scope_code_id} is not an active code of the current codebook.")
        scope = {scope_code_id}
    else:
        scope = active
    if not scope:
        raise RunConflict("The current codebook has no active codes.")
    stale = sorted(scope & embeddings.stale_code_ids(db_t))
    if stale:
        raise RunConflict(f"Codes {stale} have no current query embedding; run POST /embeddings first.")
    est = embeddings.estimate(db_c, db_t)
    if est["highlights_embedded"] < est["highlights_total"]:
        raise RunConflict(
            f"{est['highlights_total'] - est['highlights_embedded']} highlights are not embedded; "
            "run POST /embeddings first."
        )


def start_run(scope_code_id: int | None) -> int:
    """Creates a running SearchRun and searches on a daemon thread with its own sessions."""
    with _start_lock:
        with CorticoSession() as db_c, TaggingSession() as db_t:
            _check_can_start(db_c, db_t, scope_code_id)
            run_id = create_run(db_t, scope_code_id)
        thread = threading.Thread(target=_background, args=(run_id,), daemon=True, name=f"search-run-{run_id}")
        _threads[run_id] = thread
        thread.start()
    return run_id


def _background(run_id: int) -> None:
    with CorticoSession() as db_c, TaggingSession() as db_t:
        try:
            client = HttpJevClient()
        except Exception as exc:
            run = db_t.get(SearchRun, run_id)
            run.status, run.finished_at = "failed", _now()
            run.summary = {"error": f"{type(exc).__name__}: {exc}"}
            db_t.commit()
            return
        try:
            run_search(db_c, db_t, run_id, client, random.Random())
        except Exception:
            log.exception("Search run %s failed", run_id)


def join(run_id: int, timeout: float | None = None) -> None:
    thread = _threads.get(run_id)
    if thread is not None:
        thread.join(timeout)


def fail_orphaned_runs(db_t: Session) -> None:
    """Runs left `running` by a stopped server have no thread; mark them failed."""
    for run in db_t.scalars(select(SearchRun).where(SearchRun.status == "running")):
        if run.id in _threads and _threads[run.id].is_alive():
            continue
        run.status, run.finished_at = "failed", _now()
        run.summary = {**(run.summary or {}), "error": "The server stopped during the run."}
    db_t.commit()
