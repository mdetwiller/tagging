from __future__ import annotations

import random
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, Coding, HighlightMark, RunHighlight, SearchRun
from app.models_cortico import Highlight
from app.services import drift, judgments, marks, search_run
from app.services.jev import MAX_HUMAN_EXAMPLES


class FakeJev:
    def __init__(self, score):
        self.score = score
        self.bodies: list[dict] = []
        self._lock = threading.Lock()

    def judge(self, body: dict) -> dict:
        with self._lock:
            self.bodies.append(body)
        hid = body["state"]["highlight"]["id"]
        return {
            "answers": {k: {"type": "noul", "noul": self.score(hid, int(k[1:]))} for k in body["questions"]},
            "usage": {"input_tokens": 1},
        }


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, int, str]]:
    with CorticoSession() as s:
        return list(
            s.execute(select(Highlight.id, Highlight.conversation_id, Highlight.text).order_by(Highlight.id).limit(40))
        )


@pytest.fixture
def codes() -> dict[str, int]:
    with TaggingSession() as s:
        book = Codebook(name="marks test")
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
def db():
    with TaggingSession() as s:
        yield s
        # Facilitator marks apply to every code, so un-mark them for the tests that follow.
        s.rollback()
        marked = list(marks.facilitator_ids(s))
        if marked:
            judgments.set_facilitator(s, marked, False)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _human(db, code_id: int, h, status: str) -> None:
    db.add(Coding(code_id=code_id, highlight_id=h[0], conversation_id=h[1], status=status,
                  is_ai_generated=False, created_by=101))
    db.commit()


def _ai(db, code_id: int, h, p: float, run_id: int | None = None) -> None:
    db.add(Coding(code_id=code_id, highlight_id=h[0], conversation_id=h[1], status="suggested",
                  is_ai_generated=True, ai_probability=p, created_by=101, run_id=run_id))
    db.commit()


def _human_rows(db, code_id: int, hid: int) -> list[str]:
    return list(
        db.scalars(
            select(Coding.status)
            .where(Coding.code_id == code_id, Coding.highlight_id == hid, Coding.is_ai_generated.is_(False))
            .order_by(Coding.id)
        )
    )


def test_star_accepts_and_unstar_keeps_the_accept(client, db, codes, highlights):
    a, h = codes["A"], highlights[0]
    res = client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": True})
    assert res.status_code == 200 and res.json()["starred"] is True
    db.expire_all()
    assert _human_rows(db, a, h[0]) == ["applied"]
    assert marks.starred_ids(db, a) == [h[0]]
    assert marks.starred_ids(db, codes["B"]) == []

    client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": False})
    db.expire_all()
    assert marks.starred_ids(db, a) == []
    assert _human_rows(db, a, h[0]) == ["applied"]
    # Un-starring is a new row; nothing is deleted.
    assert db.scalar(select(func.count(HighlightMark.id)).where(HighlightMark.code_id == a)) == 2


def test_star_on_an_accepted_highlight_adds_no_judgment(client, db, codes, highlights):
    a, h = codes["A"], highlights[1]
    _human(db, a, h, "applied")
    client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": True})
    # Starring twice is a no-op.
    client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": True})
    db.expire_all()
    assert _human_rows(db, a, h[0]) == ["applied"]
    assert db.scalar(select(func.count(HighlightMark.id)).where(HighlightMark.code_id == a)) == 1


def test_rejecting_a_starred_highlight_unstars_it(client, db, codes, highlights):
    a, h = codes["A"], highlights[2]
    client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": True})
    res = client.post("/judgments", json={"code_id": a, "highlight_ids": [h[0]], "applied": False})
    assert res.status_code == 200
    db.expire_all()
    assert marks.starred_ids(db, a) == []
    assert _human_rows(db, a, h[0]) == ["applied", "not_applied"]


def test_cannot_star_a_facilitator_turn(client, db, codes, highlights):
    a, h = codes["A"], highlights[3]
    client.post("/facilitator", json={"highlight_ids": [h[0]], "facilitator": True})
    res = client.post("/stars", json={"code_id": a, "highlight_id": h[0], "starred": True})
    assert res.status_code == 409
    db.expire_all()
    assert _human_rows(db, a, h[0]) == []


def test_star_and_facilitator_404s(client, codes, highlights):
    assert client.post("/stars", json={"code_id": 999999, "highlight_id": highlights[0][0], "starred": True}).status_code == 404
    assert client.post("/stars", json={"code_id": codes["A"], "highlight_id": -5, "starred": True}).status_code == 404
    assert client.post("/facilitator", json={"highlight_ids": [-5], "facilitator": True}).status_code == 404


def test_facilitator_turn_leaves_review_and_agreement_and_comes_back(client, db, codes, highlights):
    a = codes["A"]
    run_id = search_run.create_run(db, None)
    run = db.get(SearchRun, run_id)
    run.status = "done"
    run.summary = {"kind": "sample", "codes": {str(a): {"positives": 2, "tries": 3, "few": True}}}
    db.commit()
    agree, disagree, plain = highlights[4], highlights[5], highlights[6]
    _ai(db, a, agree, 0.9, run_id)
    _ai(db, a, disagree, 0.9, run_id)
    _ai(db, a, plain, 0.1, run_id)
    _human(db, a, agree, "applied")
    _human(db, a, disagree, "not_applied")
    assert judgments.disagreements(db, a) == [disagree[0]]
    assert judgments.agreement(db, a)["n"] == 2

    res = client.post("/facilitator", json={"highlight_ids": [disagree[0]], "facilitator": True})
    assert res.json() == {"changed": 1, "facilitator": True}
    body = client.get(f"/codes/{a}/review").json()
    listed = [r["highlight_id"] for key in ("found", "random", "below_threshold") for r in body[key]]
    assert disagree[0] not in listed and agree[0] in listed and plain[0] in listed
    assert [r["highlight_id"] for r in body["facilitator"]] == [disagree[0]]
    assert body["facilitator"][0]["facilitator"] is True
    assert body["agreement"]["n"] == 1
    db.expire_all()
    assert judgments.disagreements(db, a) == []
    assert disagree[0] not in drift._judged(db, a)
    assert disagree[0] not in [hid for hid, _, _ in drift._judged_examples(db, db.get(Code, a), [])]
    # Facilitator turns show in every code's review.
    other = client.get(f"/codes/{codes['B']}/review").json()
    assert [r["highlight_id"] for r in other["facilitator"]] == [disagree[0]]

    client.post("/facilitator", json={"highlight_ids": [disagree[0]], "facilitator": False})
    body = client.get(f"/codes/{a}/review").json()
    assert body["facilitator"] == []
    assert body["agreement"]["n"] == 2
    # The human judgment made before marking was kept.
    assert _human_rows(db, a, disagree[0]) == ["not_applied"]


def test_facilitator_turns_are_not_sampled_searched_or_examples(db, codes, highlights, monkeypatch):
    a = codes["A"]
    monkeypatch.setattr(get_config().search, "jev_concurrency", 1)
    monkeypatch.setattr(get_config().search, "sample_size", 10_000)
    marked = highlights[7]
    _human(db, a, marked, "applied")
    judgments.set_facilitator(db, [marked[0]], True)

    db_c = CorticoSession()
    try:
        client = FakeJev(lambda h, c: 0.0)
        run_id = search_run.create_run(db, None)
        search_run.run_search(db_c, db, run_id, client, random.Random(0))
        sent = {b["state"]["highlight"]["id"] for b in client.bodies}
        assert marked[0] not in sent and len(sent) > 1
        assert " ".join(marked[2].split()[:10]) not in client.bodies[0]["questions"][f"c{a}"]["instructions"]

        # Find more ranks the marked highlight first; it is still skipped.
        client = FakeJev(lambda h, c: 0.0)
        run_id = search_run.create_run(db, a)
        rank = lambda code_id: [marked[0]] + [h[0] for h in highlights if h != marked]  # noqa: E731
        search_run.run_search(db_c, db, run_id, client, random.Random(0), rank=rank)
        queued = set(db.scalars(select(RunHighlight.highlight_id).where(RunHighlight.run_id == run_id)))
        assert marked[0] not in queued and queued
    finally:
        db_c.close()


def test_starred_highlights_are_pinned_fits_examples(db, codes, highlights):
    a = codes["A"]
    starred = highlights[8]
    judgments.star(db, a, starred[0], True)
    # More newer accepts than MAX_HUMAN_EXAMPLES would push an ordinary accept out.
    for h in highlights[9 : 10 + MAX_HUMAN_EXAMPLES]:
        _human(db, a, h, "applied")
    texts = {h[0]: h[2] for h in highlights}
    instructions = search_run.code_instructions(db, db.get(Code, a), texts)
    fits = instructions.split("Examples that fit:")[1]
    first = " ".join(starred[2].split()[:10])
    assert fits.count(first) == 1
    # Left out when scoring that same highlight.
    scored = search_run.code_instructions(db, db.get(Code, a), texts, exclude_highlight=starred[0])
    assert first not in scored
    # A facilitator turn's star is ignored while marked.
    judgments.set_facilitator(db, [starred[0]], True)
    assert first not in search_run.code_instructions(db, db.get(Code, a), texts)


def test_export_lists_stars_and_facilitator_turns(client, db, codes, highlights):
    a = codes["A"]
    judgments.star(db, a, highlights[20][0], True)
    judgments.set_facilitator(db, [highlights[21][0]], True)
    x = client.get("/export").json()["x_tagging"]
    assert x["codes"][str(a)]["starred_highlight_ids"] == [highlights[20][0]]
    assert [t["highlight_id"] for t in x["facilitator_turns"]] == [highlights[21][0]]
    assert x["facilitator_turns"][0]["marked_at"].endswith("+00:00")
