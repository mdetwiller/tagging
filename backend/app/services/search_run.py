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
from ..models import Code, CodeExemplar, Coding, DriftProposal, RunHighlight, SearchRun
from ..models_cortico import Highlight
from . import embeddings, marks, rules
from .embeddings import rank_candidates
from .jev import (
    MAX_HUMAN_EXAMPLES,
    HttpJevClient,
    JevClient,
    JevError,
    build_instructions,
    build_request,
    parse_noul,
)

log = logging.getLogger(__name__)

Rank = Callable[[int], list[int]]

# A request refused with one of these will be refused again, so the run stops at once.
FATAL_JEV_STATUSES = frozenset({400, 401, 403})
# Batches in which every request failed (after the client's retries) before the run stops.
MAX_CONSECUTIVE_FAILED_BATCHES = 3


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


def _human_examples(
    db_t: Session,
    code_id: int,
    texts: dict[int, str],
    exclude_highlight: int | None = None,
    skip: set[int] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Texts of the newest highlights whose latest human coding for the code is applied or
    not_applied, newest first, leaving out exclude_highlight, facilitator turns and `skip`."""
    rows = db_t.execute(
        select(Coding.highlight_id, Coding.status)
        .where(Coding.code_id == code_id, Coding.is_ai_generated.is_(False))
        .order_by(Coding.id.desc())
    ).all()
    latest: dict[int, str] = {}
    for hid, status in rows:
        latest.setdefault(hid, status)
    latest.pop(exclude_highlight, None)
    for hid in marks.facilitator_ids(db_t) | skip:
        latest.pop(hid, None)
    accepted = [texts[h] for h, s in latest.items() if s == "applied" and h in texts]
    rejected = [texts[h] for h, s in latest.items() if s == "not_applied" and h in texts]
    return accepted[:MAX_HUMAN_EXAMPLES], rejected[:MAX_HUMAN_EXAMPLES]


def code_instructions(
    db_t: Session,
    code: Code,
    texts: dict[int, str],
    exclude_highlight: int | None = None,
    override: dict | None = None,
) -> str:
    """The Noul instructions Jev sees for the code now. `texts` maps highlight_id to text and
    must cover the code's human-judged highlights. `override` is shaped like a drift proposal's
    `proposed` (any of name, definition, threshold, include, exclude); its keys win over the
    code's current values. Threshold is not part of the text. Starred highlights are always
    "fits" examples, after the imported exemplars."""
    override = override or {}
    exemplars = list(
        db_t.scalars(select(CodeExemplar).where(CodeExemplar.code_id == code.id).order_by(CodeExemplar.id))
    )
    facilitator = marks.facilitator_ids(db_t)
    starred_ids = [
        h for h in marks.starred_ids(db_t, code.id) if h != exclude_highlight and h not in facilitator and h in texts
    ]
    # Oldest stars first, so lowering the config limit keeps the earliest ones.
    starred_ids = starred_ids[: get_config().jev.max_starred_examples]
    starred = [texts[h] for h in starred_ids]
    accepted, rejected = _human_examples(db_t, code.id, texts, exclude_highlight, skip=set(starred_ids))
    include, exclude = rules.current_rules(db_t, code.id)
    return build_instructions(
        code,
        exemplars,
        accepted,
        rejected,
        starred=starred,
        include=override.get("include", include),
        exclude=override.get("exclude", exclude),
        name=override.get("name"),
        definition=override.get("definition"),
    )


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


SAMPLE = "sample"
FIND_MORE = "find_more"


def run_kind(scope_code_id: int | None) -> str:
    return SAMPLE if scope_code_id is None else FIND_MORE


def create_run(db_t: Session, scope_code_id: int | None) -> int:
    run = SearchRun(
        scope_code_id=scope_code_id, status="running", requests=0, tokens_used=0,
        summary={"kind": run_kind(scope_code_id)},
    )
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
    """Runs an existing `running` SearchRun: a sample run when it has no scope code, else a
    find-more run for that code. On any error the run is marked failed, rows already
    committed stay, and the error is re-raised."""
    try:
        run = db_t.get(SearchRun, run_id)
        if run.scope_code_id is None:
            _sample(db_c, db_t, run_id, client, rng)
        else:
            _find_more(db_c, db_t, run_id, run.scope_code_id, client, rng, rank)
    except Exception as exc:
        db_t.rollback()
        run = db_t.get(SearchRun, run_id)
        run.status = "failed"
        run.finished_at = _now()
        run.summary = {**(run.summary or {}), "error": f"{type(exc).__name__}: {exc}"}
        db_t.commit()
        raise


class _Judging:
    """The request and write loop both run kinds share: one Jev request per highlight against
    every active code, one AI coding row per answer, a commit per highlight, and the failure
    rules. `summarize` builds the run's summary from this object."""

    def __init__(
        self,
        db_c: Session,
        db_t: Session,
        run_id: int,
        client: JevClient,
        pool: ThreadPoolExecutor,
        extra_code_ids: list[int],
        summarize: Callable[[_Judging], dict],
    ) -> None:
        config = get_config()
        self.db_t, self.run_id, self.client, self.pool = db_t, run_id, client, pool
        self.model, self.user_id = config.jev.model, config.user_id
        self.summarize = summarize
        self.highlights = {
            hid: (text, conversation_id)
            for hid, text, conversation_id in db_c.execute(
                select(Highlight.id, Highlight.text, Highlight.conversation_id).order_by(Highlight.id)
            )
        }
        # Facilitator turns are never sampled or searched.
        facilitator = marks.facilitator_ids(db_t)
        self.all_ids = [h for h in self.highlights if h not in facilitator]
        texts = {hid: text for hid, (text, _) in self.highlights.items()}
        # Snapshotted once; codes retired mid-run keep being judged and written under these ids.
        judge = [_snapshot(db_t, c, texts) for c in embeddings.current_active_codes(db_t)]
        self.by_id = {s.id: s for s in judge}
        self.judge_ids = [s.id for s in judge]
        self.questions = [(s.id, s.instructions) for s in judge]
        counted = sorted(set(self.judge_ids) | set(extra_code_ids))
        self.human_judged = _human_judged(db_t, counted)
        # Positives credit only highlights not already human-judged for the code.
        self.positives = {c: 0 for c in counted}
        # Highlights Jev answered for the code in this run.
        self.answered = {c: 0 for c in counted}
        self.missing_answers = 0
        self.failed_requests = 0
        self.failed_batches = 0
        self.requests = 0
        self.tokens = 0

    def save(self) -> None:
        run = self.db_t.get(SearchRun, self.run_id)
        run.requests, run.tokens_used, run.summary = self.requests, self.tokens, self.summarize(self)
        self.db_t.commit()

    def judge_batch(self, hids: list[int]) -> None:
        futures = [
            self.pool.submit(
                self.client.judge,
                build_request({"id": hid, "text": self.highlights[hid][0]}, self.questions, self.model),
            )
            for hid in hids
        ]
        error: JevError | None = None
        batch_failures = 0
        for hid, future in zip(hids, futures):
            try:
                response = future.result()
            except JevError as exc:
                if exc.status in FATAL_JEV_STATUSES:
                    raise
                log.warning("Run %s: Jev request for highlight %s failed: %s", self.run_id, hid, exc)
                error = exc
                batch_failures += 1
                self.failed_requests += 1
                continue
            self.requests += 1
            self.tokens += int((response.get("usage") or {}).get("input_tokens") or 0)
            probabilities, missing = parse_noul(response, self.judge_ids)
            self.missing_answers += len(missing)
            for code_id, p in probabilities.items():
                snap = self.by_id[code_id]
                self.db_t.add(
                    Coding(
                        code_id=code_id, highlight_id=hid,
                        conversation_id=self.highlights[hid][1],
                        is_ai_generated=True, ai_probability=p, status="suggested",
                        created_by=self.user_id, run_id=self.run_id,
                        ai_metadata={
                            "model": self.model,
                            "run_id": self.run_id,
                            "question_instructions_hash": snap.instructions_hash,
                        },
                    )
                )
                self.answered[code_id] += 1
                if p >= snap.threshold and hid not in self.human_judged[code_id]:
                    self.positives[code_id] += 1
            self.save()
        self.failed_batches = self.failed_batches + 1 if batch_failures == len(hids) else 0
        if batch_failures:
            self.save()
        if self.failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
            raise error

    def finish(self) -> None:
        run = self.db_t.get(SearchRun, self.run_id)
        run.status = "done"
        run.finished_at = _now()
        self.save()


def _counts(j: _Judging) -> dict:
    return {"missing_answers": j.missing_answers, "failed_requests": j.failed_requests}


def draw_sample(all_ids: list[int], size: int, rng: random.Random) -> list[int]:
    return rng.sample(all_ids, min(size, len(all_ids)))


def _sample(db_c: Session, db_t: Session, run_id: int, client: JevClient, rng: random.Random) -> None:
    """Judges `search.sample_size` random highlights against every active code. Writes no
    run_highlight rows; the run's highlights are its AI coding rows. A code is listed in the
    summary only once Jev has answered for it."""
    cfg = get_config().search

    def summarize(j: _Judging) -> dict:
        return {
            "kind": SAMPLE,
            "codes": {
                str(c): {
                    "positives": j.positives[c],
                    "tries": j.answered[c],
                    "few": j.positives[c] < cfg.hard_to_find_below,
                }
                for c in j.judge_ids
                if j.answered[c] > 0
            },
            **_counts(j),
        }

    with ThreadPoolExecutor(max_workers=max(1, cfg.jev_concurrency)) as pool:
        judging = _Judging(db_c, db_t, run_id, client, pool, [], summarize)
        sample = draw_sample(judging.all_ids, cfg.sample_size, rng)
        size = max(1, cfg.jev_concurrency)
        for start in range(0, len(sample), size):
            judging.judge_batch(sample[start : start + size])
        judging.finish()


def _find_more(
    db_c: Session,
    db_t: Session,
    run_id: int,
    code: int,
    client: JevClient,
    rng: random.Random,
    rank: Rank,
) -> None:
    """Walks the code's candidates (similarity order, every `random_every`-th slot random)
    until `target_positives` or `max_tries_per_code`."""
    cfg = get_config().search
    random_every = round(1 / cfg.random_share) if cfg.random_share > 0 else 0
    tries = 0

    def summarize(j: _Judging) -> dict:
        return {
            "kind": FIND_MORE,
            "codes": {
                str(code): {
                    "positives": j.positives[code],
                    "tries": tries,
                    "hard_to_find": j.positives[code] < cfg.hard_to_find_below,
                }
            },
            **_counts(j),
        }

    with ThreadPoolExecutor(max_workers=max(1, cfg.jev_concurrency)) as pool:
        judging = _Judging(db_c, db_t, run_id, client, pool, [code], summarize)
        excluded = judging.human_judged[code] | (set(judging.highlights) - set(judging.all_ids))
        queue = _queue(rank(code), judging.all_ids, excluded, random_every, rng)
        while judging.positives[code] < cfg.target_positives and tries < cfg.max_tries_per_code:
            size = min(cfg.jev_concurrency, cfg.max_tries_per_code - tries)
            batch = [item for _, item in zip(range(size), queue)]
            if not batch:
                break
            for hid, picked_by, position in batch:
                tries += 1
                db_t.add(
                    RunHighlight(
                        run_id=run_id, target_code_id=code, highlight_id=hid,
                        picked_by=picked_by, position=position,
                    )
                )
            db_t.commit()
            judging.judge_batch([hid for hid, _, _ in batch])
        judging.finish()


# Guards check-and-start of every Jev job (search runs and drift proposal scores), so at most
# one runs at a time.
_start_lock = threading.Lock()
_threads: dict[int, threading.Thread] = {}


def check_no_jev_job(db_t: Session) -> None:
    """Raises RunConflict while a search run or a proposal score is running. Call it under
    _start_lock."""
    if db_t.scalars(select(SearchRun.id).where(SearchRun.status == "running")).first() is not None:
        raise RunConflict("A search run is already running.")
    if db_t.scalars(select(DriftProposal.id).where(DriftProposal.score_status == "running")).first() is not None:
        raise RunConflict("A proposal score with Jev is running.")


def _check_can_start(db_c: Session, db_t: Session, scope_code_id: int | None) -> None:
    """A sample run (no scope code) needs only active codes; a find-more run also needs the
    code's query embedding and every highlight embedded, because it ranks by similarity."""
    check_no_jev_job(db_t)
    active = {c.id for c in embeddings.current_active_codes(db_t)}
    if scope_code_id is None:
        if not active:
            raise RunConflict("The current codebook has no active codes.")
        return
    if db_t.get(Code, scope_code_id) is None:
        raise ScopeCodeNotFound(f"Code {scope_code_id} does not exist.")
    if scope_code_id not in active:
        raise RunConflict(f"Code {scope_code_id} is not an active code of the current codebook.")
    if scope_code_id in embeddings.stale_code_ids(db_t):
        raise RunConflict(f"Code {scope_code_id} has no current query embedding; embed highlights and codes first.")
    est = embeddings.estimate(db_c, db_t)
    if est["highlights_embedded"] < est["highlights_total"]:
        raise RunConflict(
            f"{est['highlights_total'] - est['highlights_embedded']} highlights are not embedded; "
            "embed highlights and codes first."
        )


def start_run(scope_code_id: int | None) -> int:
    """Creates a running SearchRun (a sample run when scope_code_id is None) and runs it on a
    daemon thread with its own sessions."""
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
            run.summary = {**(run.summary or {}), "error": f"{type(exc).__name__}: {exc}"}
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
