from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, Coding, DriftProposal, ProposalScore, SearchRun
from app.models_cortico import Highlight
from app.services import drift, judgments, search_run
from app.services.jev import JevError

PROPOSED_RULE = "Names a public official or office"


class FakeJev:
    """Answers from score(highlight_id, key) where key is "cur" or "new"; None means no answer."""

    def __init__(self, score, fail_calls=(), fail_status: int = 503, block: threading.Event | None = None):
        self.score = score
        self.fail_calls = set(fail_calls)
        self.fail_status = fail_status
        self.block = block
        self.bodies: list[dict] = []
        self._lock = threading.Lock()

    def judge(self, body: dict) -> dict:
        with self._lock:
            self.bodies.append(body)
            n = len(self.bodies)
        if self.block is not None:
            self.block.wait(30)
        if n in self.fail_calls:
            raise JevError(f"Jev returned {self.fail_status}: unavailable", self.fail_status)
        hid = body["state"]["highlight"]["id"]
        answers = {}
        for key in body["questions"]:
            p = self.score(hid, key)
            if p is not None:
                answers[key] = {"type": "noul", "noul": p}
        return {"answers": answers, "usage": {"input_tokens": 7}}


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, int, str]]:
    with CorticoSession() as s:
        return list(
            s.execute(select(Highlight.id, Highlight.conversation_id, Highlight.text).order_by(Highlight.id).limit(12))
        )


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture(autouse=True)
def concurrency(monkeypatch):
    monkeypatch.setattr(get_config().search, "jev_concurrency", 2)


@pytest.fixture
def setup(db, highlights) -> dict:
    """Code A (threshold 0.5) with eight human-judged highlights (0-3 applied, 4-7 not applied;
    highlight 3 was first rejected, then applied), an AI-only highlight 8, and a suggested
    proposal that raises the threshold to 0.7 and adds an include rule."""
    book = Codebook(name="drift score test")
    db.add(book)
    db.flush()
    code = Code(codebook_id=book.id, name="Feeling unheard", definition="People in power do not listen.")
    db.add(code)
    db.commit()
    hids = [h[0] for h in highlights]
    judgments.judge(db, code.id, [hids[3]], False)
    judgments.judge(db, code.id, hids[0:4], True)
    judgments.judge(db, code.id, hids[4:8], False)
    db.add(
        Coding(
            code_id=code.id, highlight_id=hids[8], conversation_id=highlights[8][1], is_ai_generated=True,
            ai_probability=0.9, status="suggested", created_by=get_config().user_id,
        )
    )
    proposal = DriftProposal(
        code_id=code.id, status="suggested",
        base={"name": code.name, "definition": code.definition, "threshold": 0.5, "include": [], "exclude": []},
        proposed={"threshold": 0.7, "include": [PROPOSED_RULE]},
    )
    db.add(proposal)
    db.commit()
    return {"code": code.id, "proposal": proposal.id, "hids": hids, "texts": {h[0]: h[2] for h in highlights}}


# cur and new probabilities per judged highlight index; applied are 0-3.
CUR = [0.6, 0.6, 0.4, 0.8, 0.6, 0.3, 0.2, 0.55]
NEW = [0.75, 0.65, 0.9, 0.8, 0.6, 0.3, 0.72, 0.5]


def _score_fn(setup):
    index = {h: i for i, h in enumerate(setup["hids"][:8])}
    return lambda h, key: (CUR if key == "cur" else NEW)[index[h]]


def _run_score(db, setup, client) -> DriftProposal:
    with CorticoSession() as db_c:
        drift.score_proposal(db_c, db, setup["proposal"], client)
    db.expire_all()
    return db.get(DriftProposal, setup["proposal"])


def _codings(db) -> int:
    return db.scalar(select(func.count()).select_from(Coding))


def _first_words(text: str) -> str:
    return " ".join(text.split()[:10])


def test_one_request_per_judged_highlight_with_cur_and_new(db, setup):
    client = FakeJev(_score_fn(setup))
    p = _run_score(db, setup, client)
    assert p.score_status == "done"
    asked = sorted(b["state"]["highlight"]["id"] for b in client.bodies)
    assert asked == sorted(setup["hids"][:8])
    for body in client.bodies:
        assert set(body["questions"]) == {"cur", "new"}
        assert body["model"] == get_config().jev.model
        assert PROPOSED_RULE in body["questions"]["new"]["instructions"]
        assert PROPOSED_RULE not in body["questions"]["cur"]["instructions"]


def test_scored_highlight_text_absent_from_both_questions(db, setup):
    client = FakeJev(_score_fn(setup))
    _run_score(db, setup, client)
    texts = setup["texts"]
    judged = setup["hids"][:8]
    for body in client.bodies:
        hid = body["state"]["highlight"]["id"]
        for key in ("cur", "new"):
            instructions = body["questions"][key]["instructions"]
            assert _first_words(texts[hid]) not in instructions
            # Other judged highlights still serve as examples, so the absence above is not vacuous.
            assert any(_first_words(texts[o]) in instructions for o in judged if o != hid), key


def test_summary_matches_hand_computed_stats(db, setup):
    before_count = _codings(db)
    p = _run_score(db, setup, FakeJev(_score_fn(setup)))
    human = [True] * 4 + [False] * 4
    before = judgments.agreement_stats(zip(human, [x >= 0.5 for x in CUR]))
    after = judgments.agreement_stats(zip(human, [x >= 0.7 for x in NEW]))
    assert p.score_summary == {"before": before, "after": after, "missing_answers": 0, "failed_requests": 0}
    # The proposed threshold matters: with the current one, "after" would differ.
    assert after != judgments.agreement_stats(zip(human, [x >= 0.5 for x in NEW]))

    rows = db.scalars(select(ProposalScore).where(ProposalScore.proposal_id == p.id)).all()
    by_h = {r.highlight_id: r for r in rows}
    assert len(rows) == 8
    for i, h in enumerate(setup["hids"][:8]):
        assert (by_h[h].human, by_h[h].current_p, by_h[h].proposed_p) == (
            "applied" if i < 4 else "not_applied", CUR[i], NEW[i],
        )
    assert _codings(db) == before_count


def test_after_uses_current_threshold_without_proposed_one(db, setup):
    p = db.get(DriftProposal, setup["proposal"])
    p.proposed = {"include": [PROPOSED_RULE]}
    db.commit()
    p = _run_score(db, setup, FakeJev(_score_fn(setup)))
    human = [True] * 4 + [False] * 4
    assert p.score_summary["after"] == judgments.agreement_stats(zip(human, [x >= 0.5 for x in NEW]))


def test_missing_answer_counted_and_stored_null(db, setup):
    first = setup["hids"][0]
    base = _score_fn(setup)
    p = _run_score(db, setup, FakeJev(lambda h, key: None if (h == first and key == "new") else base(h, key)))
    assert p.score_summary["missing_answers"] == 1
    assert p.score_summary["before"]["n"] == 8
    assert p.score_summary["after"]["n"] == 7
    row = db.scalars(select(ProposalScore).where(ProposalScore.highlight_id == first, ProposalScore.proposal_id == p.id)).one()
    assert row.current_p == CUR[0] and row.proposed_p is None


def test_single_failed_request_continues(db, setup):
    p = _run_score(db, setup, FakeJev(_score_fn(setup), fail_calls={2}))
    assert p.score_status == "done"
    assert p.score_summary["failed_requests"] == 1
    assert p.score_summary["before"]["n"] == 7


def test_401_fails_score(db, setup):
    with pytest.raises(JevError):
        _run_score(db, setup, FakeJev(_score_fn(setup), fail_calls={1}, fail_status=401))
    db.expire_all()
    p = db.get(DriftProposal, setup["proposal"])
    assert p.score_status == "failed"
    assert "401" in p.score_summary["error"]


def test_three_all_failed_batches_fail_score(db, setup):
    with pytest.raises(JevError):
        _run_score(db, setup, FakeJev(_score_fn(setup), fail_calls=range(1, 100)))
    db.expire_all()
    p = db.get(DriftProposal, setup["proposal"])
    assert p.score_status == "failed"
    # Concurrency 2: three batches of two, all failed.
    assert p.score_summary["failed_requests"] == 6
    assert "503" in p.score_summary["error"]


def test_rescore_appends_rows_and_replaces_summary(db, setup):
    _run_score(db, setup, FakeJev(_score_fn(setup)))
    p = _run_score(db, setup, FakeJev(lambda h, key: 0.0))
    rows = db.scalar(select(func.count()).select_from(ProposalScore).where(ProposalScore.proposal_id == p.id))
    assert rows == 16
    assert p.score_summary["before"]["n"] == 8
    assert p.score_summary["before"]["both_no"] == 4 and p.score_summary["before"]["human_only"] == 4


# Routes and the shared job guard


@pytest.fixture
def fake_http(monkeypatch):
    def install(client):
        monkeypatch.setattr(drift, "HttpJevClient", lambda: client)
        return client

    return install


def test_post_score_runs_in_background(db, setup, fake_http):
    fake_http(FakeJev(_score_fn(setup)))
    with TestClient(app) as c:
        resp = c.post(f"/proposals/{setup['proposal']}/score")
        assert resp.status_code == 202, resp.text
        drift.join_score(setup["proposal"], timeout=30)
        body = c.get(f"/proposals/{setup['proposal']}").json()
    assert body["id"] == setup["proposal"]
    assert body["score_status"] == "done"
    assert set(body["score_summary"]) == {"before", "after", "missing_answers", "failed_requests"}
    assert body["score_summary"]["before"]["n"] == 8


def test_unknown_proposal_404():
    with TestClient(app) as c:
        assert c.get("/proposals/999999").status_code == 404
        assert c.post("/proposals/999999/score").status_code == 404


def test_only_suggested_proposal_can_be_scored(db, setup):
    p = db.get(DriftProposal, setup["proposal"])
    p.status = "dismissed"
    db.commit()
    with TestClient(app) as c:
        resp = c.post(f"/proposals/{setup['proposal']}/score")
    assert resp.status_code == 409


def test_score_409_while_search_run_running(db, setup):
    with TestClient(app) as c:
        running = SearchRun(status="running")
        db.add(running)
        db.commit()
        try:
            resp = c.post(f"/proposals/{setup['proposal']}/score")
            assert resp.status_code == 409
        finally:
            running.status = "failed"
            db.commit()
    assert db.get(DriftProposal, setup["proposal"]).score_status == "none"


def test_search_run_409_while_score_running(db, setup, fake_http):
    gate = threading.Event()
    fake_http(FakeJev(_score_fn(setup), block=gate))
    with TestClient(app) as c:
        try:
            assert c.post(f"/proposals/{setup['proposal']}/score").status_code == 202
            resp = c.post("/runs", json={})
            assert resp.status_code == 409
            assert "score" in resp.json()["detail"].lower()
            # A second score, of this or any proposal, waits too.
            assert c.post(f"/proposals/{setup['proposal']}/score").status_code == 409
        finally:
            gate.set()
            drift.join_score(setup["proposal"], timeout=30)
    db.expire_all()
    assert db.get(DriftProposal, setup["proposal"]).score_status == "done"


def test_orphaned_running_score_marked_failed(db, setup):
    p = db.get(DriftProposal, setup["proposal"])
    p.score_status = "running"
    db.commit()
    with TestClient(app):
        pass
    db.expire_all()
    p = db.get(DriftProposal, setup["proposal"])
    assert p.score_status == "failed"
    assert "stopped" in p.score_summary["error"]


def test_early_background_error_leaves_no_running_score(db, setup, fake_http, monkeypatch):
    fake_http(FakeJev(_score_fn(setup)))

    def broken(*args, **kwargs):
        raise RuntimeError("boom before scoring")

    monkeypatch.setattr(drift, "score_proposal", broken)
    with TestClient(app) as c:
        assert c.post(f"/proposals/{setup['proposal']}/score").status_code == 202
        drift.join_score(setup["proposal"], timeout=30)
        db.expire_all()
        p = db.get(DriftProposal, setup["proposal"])
        assert p.score_status == "failed"
        assert "boom before scoring" in p.score_summary["error"]
        monkeypatch.undo()
        fake_http(FakeJev(_score_fn(setup)))
        # Nothing is stuck: a new score can start and finish.
        assert c.post(f"/proposals/{setup['proposal']}/score").status_code == 202
        drift.join_score(setup["proposal"], timeout=30)
    db.expire_all()
    assert db.get(DriftProposal, setup["proposal"]).score_status == "done"


def test_threshold_only_proposal_asks_one_question_scored_against_both_thresholds(db, setup):
    p = db.get(DriftProposal, setup["proposal"])
    p.proposed = {"threshold": 0.7}
    db.commit()
    client = FakeJev(_score_fn(setup))
    p = _run_score(db, setup, client)
    assert p.score_status == "done"
    assert len(client.bodies) == 8
    for body in client.bodies:
        assert set(body["questions"]) == {"cur"}
    human = [True] * 4 + [False] * 4
    assert p.score_summary == {
        "before": judgments.agreement_stats(zip(human, [x >= 0.5 for x in CUR])),
        "after": judgments.agreement_stats(zip(human, [x >= 0.7 for x in CUR])),
        "missing_answers": 0,
        "failed_requests": 0,
    }
    assert p.score_summary["before"] != p.score_summary["after"]
    rows = db.scalars(select(ProposalScore).where(ProposalScore.proposal_id == p.id)).all()
    assert all(r.proposed_p == r.current_p for r in rows)


def test_score_of_retired_code_is_409(db, setup):
    db.get(Code, setup["code"]).status = "RETIRED"
    db.commit()
    with TestClient(app) as c:
        resp = c.post(f"/proposals/{setup['proposal']}/score")
    assert resp.status_code == 409 and "retired" in resp.json()["detail"]
    db.expire_all()
    assert db.get(DriftProposal, setup["proposal"]).score_status == "none"
