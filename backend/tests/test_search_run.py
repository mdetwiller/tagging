from __future__ import annotations

import hashlib
import random
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, Coding, RunHighlight, SearchRun
from app.models_cortico import Highlight
from app.schemas import CodeEdit
from app.services import embeddings, search_run
from app.services.codebook_edit import edit_code
from app.services.jev import JevError

DIM = 8


class FakeJev:
    """Answers from a score function of (highlight_id, code_id); None means no answer."""

    def __init__(self, score, fail_on_call: int | None = None, on_call=None, fail_calls=(), fail_status: int = 503):
        self.score = score
        self.fail_on_call = fail_on_call
        self.fail_calls = set(fail_calls)
        self.fail_status = fail_status
        self.on_call = on_call
        self.bodies: list[dict] = []
        self._lock = threading.Lock()

    def judge(self, body: dict) -> dict:
        with self._lock:
            self.bodies.append(body)
            n = len(self.bodies)
        if self.on_call:
            self.on_call(n)
        if (self.fail_on_call is not None and n >= self.fail_on_call) or n in self.fail_calls:
            raise JevError(f"Jev returned {self.fail_status}: unavailable", self.fail_status)
        hid = body["state"]["highlight"]["id"]
        answers = {}
        for key in body["questions"]:
            p = self.score(hid, int(key[1:]))
            if p is not None:
                answers[key] = {"type": "noul", "noul": p}
        return {"answers": answers, "usage": {"input_tokens": 7}}


@pytest.fixture(scope="module")
def highlight_ids() -> list[int]:
    with CorticoSession() as s:
        return list(s.scalars(select(Highlight.id).order_by(Highlight.id)))


@pytest.fixture
def codes() -> dict[str, int]:
    """A new current codebook with codes A and B (and an older codebook's code stays out)."""
    with TaggingSession() as s:
        book = Codebook(name="search-run test")
        s.add(book)
        s.flush()
        ids = {}
        for name in ("A", "B"):
            code = Code(codebook_id=book.id, name=name, definition=f"Definition of {name}.")
            s.add(code)
            s.flush()
            ids[name] = code.id
        s.commit()
        return ids


@pytest.fixture
def sessions():
    db_c, db_t = CorticoSession(), TaggingSession()
    try:
        yield db_c, db_t
    finally:
        db_c.close()
        db_t.close()


@pytest.fixture
def concurrency(monkeypatch):
    def set_to(n: int) -> None:
        monkeypatch.setattr(get_config().search, "jev_concurrency", n)

    set_to(1)
    return set_to


def _ranker(highlight_ids: list[int]):
    """Each code ranks every highlight, in a code-specific but fixed order."""

    def rank(code_id: int) -> list[int]:
        return sorted(highlight_ids, key=lambda h: hashlib.sha256(f"{code_id}:{h}".encode()).digest())

    return rank


def _run(sessions, scope: int | None, client, rank) -> SearchRun:
    db_c, db_t = sessions
    run_id = search_run.create_run(db_t, scope)
    search_run.run_search(db_c, db_t, run_id, client, random.Random(0), rank=rank)
    db_t.expire_all()
    return db_t.get(SearchRun, run_id)


def _summary(run: SearchRun, code_id: int) -> dict:
    return run.summary["codes"][str(code_id)]


def _targets(db_t, run_id: int, code_id: int) -> list[RunHighlight]:
    return list(
        db_t.scalars(
            select(RunHighlight)
            .where(RunHighlight.run_id == run_id, RunHighlight.target_code_id == code_id)
            .order_by(RunHighlight.position)
        )
    )


def test_stops_at_ten_positives(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.9 if c == a else 0.0)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    assert run.status == "done"
    assert _summary(run, a) == {"positives": 10, "tries": 10, "hard_to_find": False}
    assert len(_targets(sessions[1], run.id, a)) == 10
    assert run.requests == 10
    assert run.tokens_used == 70


def test_late_positives_recorded_with_concurrency(codes, sessions, highlight_ids, concurrency):
    concurrency(4)
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.9 if c == a else 0.0)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    # Batches of 4: the stop check passes at 8 positives, so a third batch runs.
    assert _summary(run, a) == {"positives": 12, "tries": 12, "hard_to_find": False}


def test_stops_at_200_tries(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.1)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    assert _summary(run, a) == {"positives": 0, "tries": 200, "hard_to_find": True}


def test_other_codes_credit(codes, sessions, highlight_ids, concurrency):
    a, b = codes["A"], codes["B"]
    # B fires on every fourth highlight, A on none. A is searched first (tie on 0, lower id).
    fires_b = set(highlight_ids[::4])
    client = FakeJev(lambda h, c: 0.9 if c == b and h in fires_b else 0.0)
    run = _run(sessions, None, client, _ranker(highlight_ids))
    sa, sb = _summary(run, a), _summary(run, b)
    assert sa["tries"] == 200 and sa["hard_to_find"] is True
    assert sb["tries"] == 0
    assert sb["positives"] >= 10 and sb["hard_to_find"] is False


def test_other_code_not_credited_for_human_judged(codes, sessions, highlight_ids, concurrency):
    a, b = codes["A"], codes["B"]
    db_c, db_t = sessions
    rank = _ranker(highlight_ids)
    h = rank(a)[0]
    db_t.add(Coding(code_id=b, highlight_id=h, conversation_id=1, status="not_applied",
                    is_ai_generated=False, created_by=101))
    db_t.commit()
    # Only (h, B) scores high. A is searched first and judges h, so B sees h only as a credit.
    client = FakeJev(lambda hid, c: 0.9 if c == b and hid == h else 0.0)
    run = _run(sessions, None, client, rank)
    assert _targets(db_t, run.id, a)[0].highlight_id == h
    assert _summary(run, b)["positives"] == 0
    ai = db_t.scalars(
        select(Coding).where(Coding.run_id == run.id, Coding.code_id == b, Coding.highlight_id == h)
    ).all()
    assert len(ai) == 1 and ai[0].is_ai_generated and ai[0].ai_probability == 0.9


def test_every_fifth_is_random(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.0)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    rows = _targets(sessions[1], run.id, a)
    assert [r.position for r in rows] == list(range(1, 201))
    for r in rows:
        assert r.picked_by == ("random" if r.position % 5 == 0 else "similarity"), r.position
    ranked = _ranker(highlight_ids)(a)
    random_ids = {r.highlight_id for r in rows if r.picked_by == "random"}
    expected = [h for h in ranked if h not in random_ids][:160]
    assert [r.highlight_id for r in rows if r.picked_by == "similarity"] == expected
    assert len({r.highlight_id for r in rows}) == 200


def test_skips_human_judged(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    db_c, db_t = sessions
    ranked = _ranker(highlight_ids)(a)
    human = ranked[:3]
    for hid, status in zip(human, ("applied", "not_applied", "applied")):
        db_t.add(Coding(code_id=a, highlight_id=hid, conversation_id=1, status=status,
                        is_ai_generated=False, created_by=101))
    db_t.commit()
    client = FakeJev(lambda h, c: 0.0)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    queued = {r.highlight_id for r in _targets(db_t, run.id, a)}
    assert queued.isdisjoint(human)
    assert _targets(db_t, run.id, a)[0].highlight_id == ranked[3]


def test_instructions_include_human_examples(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    db_c, db_t = sessions
    applied_hid, rejected_hid = highlight_ids[10], highlight_ids[11]
    db_t.add(Coding(code_id=a, highlight_id=applied_hid, conversation_id=1, status="applied",
                    is_ai_generated=False, created_by=101))
    db_t.add(Coding(code_id=a, highlight_id=rejected_hid, conversation_id=1, status="not_applied",
                    is_ai_generated=False, created_by=101))
    # An AI row never counts as a human example.
    db_t.add(Coding(code_id=a, highlight_id=highlight_ids[12], conversation_id=1, status="suggested",
                    is_ai_generated=True, created_by=101))
    db_t.commit()
    texts = {h.id: h.text for h in db_c.scalars(select(Highlight).where(Highlight.id.in_(highlight_ids[10:13])))}
    client = FakeJev(lambda h, c: 0.9)
    _run(sessions, a, client, _ranker(highlight_ids))
    instructions = client.bodies[0]["questions"][f"c{a}"]["instructions"]
    fit, not_fit = instructions.split("Examples that do not fit:")
    first_words = lambda t: " ".join(t.split()[:10])  # noqa: E731
    assert first_words(texts[applied_hid]) in fit
    assert first_words(texts[rejected_hid]) in not_fit
    assert first_words(texts[highlight_ids[12]]) not in instructions


def test_missing_answer_no_null_row(codes, sessions, highlight_ids, concurrency):
    a, b = codes["A"], codes["B"]
    client = FakeJev(lambda h, c: 0.9 if c == a else None)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    db_t = sessions[1]
    rows = list(db_t.scalars(select(Coding).where(Coding.run_id == run.id)))
    assert {r.code_id for r in rows} == {a}
    assert all(r.ai_probability is not None for r in rows)
    assert run.summary["missing_answers"] == 10


def test_ai_rows_are_suggested_with_metadata(codes, sessions, highlight_ids, concurrency):
    a, b = codes["A"], codes["B"]
    client = FakeJev(lambda h, c: 0.7 if c == a else 0.2)
    run = _run(sessions, a, client, _ranker(highlight_ids))
    db_c, db_t = sessions
    rows = list(db_t.scalars(select(Coding).where(Coding.run_id == run.id)))
    assert len(rows) == 20  # 10 highlights x 2 judge codes
    conversation_of = dict(db_c.execute(select(Highlight.id, Highlight.conversation_id)).all())
    hashes = {}
    for r in rows:
        assert r.status == "suggested" and r.is_ai_generated is True
        assert r.created_by == get_config().user_id
        assert r.conversation_id == conversation_of[r.highlight_id]
        assert r.ai_metadata["model"] == get_config().jev.model
        assert r.ai_metadata["run_id"] == run.id
        hashes.setdefault(r.code_id, set()).add(r.ai_metadata["question_instructions_hash"])
    assert all(len(h) == 1 for h in hashes.values())
    sent = client.bodies[0]["questions"]
    for code_id, (h,) in hashes.items():
        assert h == hashlib.sha256(sent[f"c{code_id}"]["instructions"].encode()).hexdigest()


def test_retire_during_run_uses_snapshot(codes, sessions, highlight_ids, concurrency):
    a, b = codes["A"], codes["B"]

    def retire_b(n: int) -> None:
        if n == 3:
            with TaggingSession() as s:
                edit_code(s, b, CodeEdit(status="RETIRED"))

    client = FakeJev(lambda h, c: 0.0, on_call=retire_b)
    run = _run(sessions, None, client, _ranker(highlight_ids))
    assert run.status == "done"
    db_t = sessions[1]
    assert db_t.get(Code, b).status == "RETIRED"
    b_rows = db_t.scalars(select(Coding).where(Coding.run_id == run.id, Coding.code_id == b)).all()
    assert len(b_rows) == 400  # B judged on every request, searched as a target too
    assert _summary(run, b)["tries"] == 200


def test_judged_highlights_not_requested_twice(codes, sessions, highlight_ids, concurrency):
    client = FakeJev(lambda h, c: 0.0)
    # Both codes rank identically, so B's queue starts with highlights A already judged.
    rank = lambda code_id: list(highlight_ids)  # noqa: E731
    run = _run(sessions, None, client, rank)
    sent = [b["state"]["highlight"]["id"] for b in client.bodies]
    assert len(sent) == len(set(sent)) == 400
    assert run.requests == 400


def test_jev_failure_marks_run_failed(codes, sessions, highlight_ids, concurrency):
    # Every call from the fourth fails, so batches 4, 5 and 6 fail in a row and the run stops.
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.0, fail_on_call=4)
    db_c, db_t = sessions
    run_id = search_run.create_run(db_t, a)
    with pytest.raises(JevError):
        search_run.run_search(db_c, db_t, run_id, client, random.Random(0), rank=_ranker(highlight_ids))
    db_t.expire_all()
    run = db_t.get(SearchRun, run_id)
    assert run.status == "failed"
    assert "503" in run.summary["error"]
    assert run.finished_at is not None
    assert run.requests == 3
    assert len(client.bodies) == 6
    assert run.summary["failed_requests"] == 3
    rows = db_t.scalars(select(Coding).where(Coding.run_id == run_id)).all()
    assert len(rows) == 6  # three answered highlights x 2 judge codes


def test_single_jev_failure_continues(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.9 if c == a else 0.0, fail_calls={2, 5, 6})
    run = _run(sessions, a, client, _ranker(highlight_ids))
    assert run.status == "done"
    assert run.summary["failed_requests"] == 3
    assert _summary(run, a) == {"positives": 10, "tries": 13, "hard_to_find": False}
    assert run.requests == 10


def test_jev_401_fails_run_at_once(codes, sessions, highlight_ids, concurrency):
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.0, fail_calls={2}, fail_status=401)
    db_c, db_t = sessions
    run_id = search_run.create_run(db_t, a)
    with pytest.raises(JevError):
        search_run.run_search(db_c, db_t, run_id, client, random.Random(0), rank=_ranker(highlight_ids))
    db_t.expire_all()
    run = db_t.get(SearchRun, run_id)
    assert run.status == "failed"
    assert "401" in run.summary["error"]
    assert len(client.bodies) == 2
    assert run.requests == 1


# Background start and routes


@pytest.fixture
def emb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TAGGING_EMBEDDINGS_DIR", str(tmp_path / "embeddings"))
    get_config.cache_clear()
    embeddings.clear_cache()
    yield
    get_config.cache_clear()
    embeddings.clear_cache()


class HashEmbedder:
    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return [[b - 128.0 for b in hashlib.sha256(t.encode()).digest()[:DIM]] for t in texts], 0


def _embed_everything() -> None:
    with CorticoSession() as db_c, TaggingSession() as db_t:
        embeddings.embed_all(db_c, db_t, HashEmbedder())


def _finish(run_id: int) -> None:
    search_run.join(run_id, timeout=30)


def test_run_without_embeddings_409(emb_dir, codes):
    with TestClient(app) as c:
        resp = c.post("/runs", json={})
    assert resp.status_code == 409
    assert "embed" in resp.json()["detail"].lower()


def test_second_run_409(emb_dir, codes):
    _embed_everything()
    with TestClient(app) as c, TaggingSession() as s:
        running = SearchRun(status="running")
        s.add(running)
        s.commit()
        try:
            resp = c.post("/runs", json={"code_id": codes["A"]})
            assert resp.status_code == 409
            assert "already running" in resp.json()["detail"]
        finally:
            running.status = "failed"
            s.commit()


def test_post_run_in_background(emb_dir, codes, highlight_ids, monkeypatch, concurrency):
    _embed_everything()
    a = codes["A"]
    client = FakeJev(lambda h, c: 0.9 if c == a else 0.0)
    monkeypatch.setattr(search_run, "HttpJevClient", lambda: client)
    with TestClient(app) as c:
        resp = c.post("/runs", json={"code_id": a})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        _finish(run_id)
        body = c.get(f"/runs/{run_id}").json()
        latest = c.get("/runs/latest").json()
    assert body["status"] == "done"
    assert body["requests"] == 10 and body["tokens_used"] == 70
    assert body["summary"]["codes"][str(a)] == {"positives": 10, "tries": 10, "hard_to_find": False}
    assert latest["id"] == run_id


def test_background_failure_marks_run_failed(emb_dir, codes, monkeypatch, concurrency):
    _embed_everything()

    def no_key():
        raise JevError("TYPESAFE_API_KEY is not set")

    monkeypatch.setattr(search_run, "HttpJevClient", no_key)
    with TestClient(app) as c:
        run_id = c.post("/runs", json={"code_id": codes["A"]}).json()["run_id"]
        _finish(run_id)
        body = c.get(f"/runs/{run_id}").json()
    assert body["status"] == "failed"
    assert "TYPESAFE_API_KEY" in body["summary"]["error"]


def test_post_run_unknown_or_retired_code(emb_dir, codes):
    _embed_everything()
    with TestClient(app) as c:
        assert c.post("/runs", json={"code_id": 10_000_000}).status_code == 404
        with TaggingSession() as s:
            edit_code(s, codes["B"], CodeEdit(status="RETIRED"))
        assert c.post("/runs", json={"code_id": codes["B"]}).status_code == 409


def test_get_unknown_run_404():
    with TestClient(app) as c:
        assert c.get("/runs/10000000").status_code == 404
