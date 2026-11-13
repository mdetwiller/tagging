from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, Coding, HighlightStatus, RunHighlight, SearchRun
from app.models_cortico import Conversation, Highlight
from app.services import judgments


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, int]]:
    """(highlight_id, conversation_id) for the first 40 snapshot highlights."""
    with CorticoSession() as s:
        return list(s.execute(select(Highlight.id, Highlight.conversation_id).order_by(Highlight.id).limit(40)))


@pytest.fixture
def codes() -> dict[str, int]:
    """A new current codebook with active codes A, B and C and a retired code R."""
    with TaggingSession() as s:
        book = Codebook(name="judgments test")
        s.add(book)
        s.flush()
        ids = {}
        for name in ("A", "B", "C", "R"):
            code = Code(
                codebook_id=book.id, name=name, definition=f"Definition of {name}.",
                status="RETIRED" if name == "R" else "ACTIVE",
            )
            s.add(code)
            s.flush()
            ids[name] = code.id
        s.commit()
        return ids


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _ai(db, code_id: int, highlight: tuple[int, int], p: float, run_id: int | None = None) -> None:
    db.add(
        Coding(
            code_id=code_id, highlight_id=highlight[0], conversation_id=highlight[1],
            is_ai_generated=True, ai_probability=p, status="suggested",
            created_by=get_config().user_id, run_id=run_id,
        )
    )
    db.commit()


def _rows(db, code_id: int, highlight_id: int, human: bool = True) -> list[Coding]:
    return list(
        db.scalars(
            select(Coding)
            .where(
                Coding.code_id == code_id, Coding.highlight_id == highlight_id,
                Coding.is_ai_generated.is_(not human),
            )
            .order_by(Coding.id)
        )
    )


def test_latest_human_wins(db, codes, highlights):
    a, h = codes["A"], highlights[0][0]
    judgments.judge(db, a, [h], True)
    judgments.judge(db, a, [h], False)
    assert judgments.final_for(db, a, h) is False
    judgments.judge(db, a, [h], True)
    assert judgments.final_for(db, a, h) is True
    rows = _rows(db, a, h)
    assert [r.status for r in rows] == ["applied", "not_applied", "applied"]
    assert all(r.created_by == get_config().user_id for r in rows)
    assert all(r.conversation_id == highlights[0][1] for r in rows)
    assert all(r.highlight_share_id is None for r in rows)


def test_human_beats_ai(db, codes, highlights):
    a = codes["A"]
    _ai(db, a, highlights[0], 0.9)
    judgments.judge(db, a, [highlights[0][0]], False)
    assert judgments.final_for(db, a, highlights[0][0]) is False


def test_ai_threshold_when_no_human(db, codes, highlights):
    a, h = codes["A"], highlights[0]
    assert judgments.final_for(db, a, h[0]) is None
    _ai(db, a, h, 0.6)
    assert judgments.final_for(db, a, h[0]) is True
    db.get(Code, a).threshold = 0.7
    db.commit()
    assert judgments.final_for(db, a, h[0]) is False


def test_latest_ai_row_wins(db, codes, highlights):
    a, h = codes["A"], highlights[0]
    _ai(db, a, h, 0.9)
    _ai(db, a, h, 0.1)
    assert judgments.final_for(db, a, h[0]) is False


def _seed_pairs(db, code_id: int, highlights, pairs: list[tuple[bool, bool]]) -> None:
    for (human, ai), h in zip(pairs, highlights):
        _ai(db, code_id, h, 0.9 if ai else 0.1)
        judgments.judge(db, code_id, [h[0]], human)


def test_kappa_known_values(db, codes, highlights):
    a = codes["A"]
    pairs = [(True, True)] * 6 + [(False, False)] * 2 + [(True, False), (False, True)]
    _seed_pairs(db, a, highlights, pairs)
    # A highlight with only an AI row and one with only a human row are left out.
    _ai(db, a, highlights[20], 0.9)
    judgments.judge(db, a, [highlights[21][0]], True)

    result = judgments.agreement(db, a)
    p_o = 8 / 10
    p_e = (7 / 10) * (7 / 10) + (3 / 10) * (3 / 10)
    assert result["n"] == 10
    assert (result["both_yes"], result["both_no"], result["human_only"], result["ai_only"]) == (6, 2, 1, 1)
    assert result["percent_match"] == pytest.approx(0.8)
    assert result["kappa"] == pytest.approx((p_o - p_e) / (1 - p_e))
    assert result["kappa"] == pytest.approx(0.5238, abs=1e-4)


def test_agreement_uses_latest_of_each(db, codes, highlights):
    a, h = codes["A"], highlights[0]
    _ai(db, a, h, 0.1)
    _ai(db, a, h, 0.9)
    judgments.judge(db, a, [h[0]], False)
    judgments.judge(db, a, [h[0]], True)
    result = judgments.agreement(db, a)
    assert (result["n"], result["both_yes"]) == (1, 1)


def test_agreement_empty(db, codes):
    result = judgments.agreement(db, codes["A"])
    assert result == {
        "n": 0, "both_yes": 0, "both_no": 0, "human_only": 0, "ai_only": 0,
        "percent_match": None, "kappa": None,
    }


def test_agreement_all_identical(db, codes, highlights):
    a = codes["A"]
    _seed_pairs(db, a, highlights, [(True, True)] * 4)
    result = judgments.agreement(db, a)
    assert result["percent_match"] == 1.0
    assert result["kappa"] is None


def test_bulk_judge_many(db, codes, highlights):
    a = codes["A"]
    ids = [h[0] for h in highlights[:3]]
    assert judgments.judge(db, a, ids, True) == 3
    count = db.scalar(
        select(func.count()).select_from(Coding).where(Coding.code_id == a, Coding.is_ai_generated.is_(False))
    )
    assert count == 3


def test_apply_other_code_from_random_row(client, db, codes, highlights):
    b, h = codes["B"], highlights[5][0]
    assert judgments.final_for(db, b, h) is None
    r = client.post("/judgments", json={"code_id": b, "highlight_ids": [h], "applied": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"written": 1}
    db.expire_all()
    assert judgments.final_for(db, b, h) is True
    assert len(_rows(db, b, h)) == 1


def test_unknown_highlight_404(client, db, codes, highlights):
    a = codes["A"]
    r = client.post("/judgments", json={"code_id": a, "highlight_ids": [highlights[0][0], 999_999_999], "applied": True})
    assert r.status_code == 404
    assert _rows(db, a, highlights[0][0]) == []


def test_unknown_code_404(client, highlights):
    r = client.post("/judgments", json={"code_id": 999_999, "highlight_ids": [highlights[0][0]], "applied": True})
    assert r.status_code == 404


def test_empty_highlight_ids_422(client, codes):
    r = client.post("/judgments", json={"code_id": codes["A"], "highlight_ids": [], "applied": True})
    assert r.status_code == 422
    r = client.post("/highlight_status", json={"highlight_ids": [], "status": "completed"})
    assert r.status_code == 422


def test_highlight_status_upserts(client, db, highlights):
    ids = [highlights[30][0], highlights[31][0]]
    r = client.post("/highlight_status", json={"highlight_ids": ids, "status": "completed"})
    assert r.status_code == 200, r.text
    r = client.post("/highlight_status", json={"highlight_ids": ids, "status": "not_started"})
    assert r.status_code == 200
    db.expire_all()
    rows = list(db.scalars(select(HighlightStatus).where(HighlightStatus.highlight_id.in_(ids))))
    assert sorted((r.highlight_id, r.status) for r in rows) == sorted((i, "not_started") for i in ids)


def test_highlight_status_rejects_unknown(client, db, highlights):
    h = highlights[32][0]
    r = client.post("/highlight_status", json={"highlight_ids": [h, 999_999_999], "status": "completed"})
    assert r.status_code == 404
    assert db.get(HighlightStatus, h) is None
    r = client.post("/highlight_status", json={"highlight_ids": [h], "status": "Done"})
    assert r.status_code == 422


def _run_rows(db, code_id: int, rows: list[tuple[int, str]]) -> int:
    run = SearchRun(scope_code_id=code_id, status="done")
    db.add(run)
    db.flush()
    for position, (hid, picked_by) in enumerate(rows, start=1):
        db.add(RunHighlight(run_id=run.id, target_code_id=code_id, highlight_id=hid, picked_by=picked_by, position=position))
    db.commit()
    return run.id


def test_review_splits_found_and_random(client, db, codes, highlights):
    a = codes["A"]
    h = [x[0] for x in highlights]
    old_run = _run_rows(db, a, [(h[10], "similarity"), (h[11], "random")])
    run = _run_rows(db, a, [(h[0], "similarity"), (h[1], "similarity"), (h[2], "random")])
    _ai(db, a, highlights[0], 0.9, run)
    _ai(db, a, highlights[1], 0.2, run)
    _ai(db, a, highlights[2], 0.3, run)
    judgments.judge(db, a, [h[1]], False)
    # A final yes from outside this run is listed under found too.
    judgments.judge(db, a, [h[15]], True)

    body = client.get(f"/codes/{a}/review").json()
    assert body["code"]["id"] == a
    assert body["run_id"] == run
    # h[1] scored 0.2 and was rejected, so it is below the threshold, not found.
    assert [r["highlight_id"] for r in body["found"]] == [h[0], h[15]]
    assert [r["highlight_id"] for r in body["below_threshold"]] == [h[1]]
    assert [r["highlight_id"] for r in body["random"]] == [h[2]]
    assert body["agreement"]["n"] == 1

    first, second = body["found"][0], body["below_threshold"][0]
    with CorticoSession() as s:
        hl = s.get(Highlight, h[0])
        project_id = s.get(Conversation, hl.conversation_id).project_id
        text = hl.text
    assert first == {
        "highlight_id": h[0], "conversation_id": hl.conversation_id, "project_id": project_id,
        "text": text, "ai_probability": 0.9, "human": None, "final": True,
        "status": first["status"],
        "other_codes": [{"code_id": codes["B"], "name": "B", "final": None}, {"code_id": codes["C"], "name": "C", "final": None}],
    }
    assert first["status"] in ("not_started", "in_progress", "completed")
    assert (second["human"], second["final"]) == ("not_applied", False)

    older = client.get(f"/codes/{a}/review", params={"run_id": old_run}).json()
    # h[0] is a final yes (AI 0.9) from the newer run, so it comes before h[15], which no run listed.
    assert [r["highlight_id"] for r in older["found"]] == [h[0], h[15]]
    assert [r["highlight_id"] for r in older["below_threshold"]] == [h[10]]
    assert [r["highlight_id"] for r in older["random"]] == [h[11]]


def test_review_below_threshold(client, db, codes, highlights):
    a = codes["A"]
    h = [x[0] for x in highlights]
    run = _run_rows(db, a, [
        (h[0], "similarity"), (h[1], "similarity"), (h[2], "similarity"),
        (h[3], "similarity"), (h[4], "random"),
    ])
    _ai(db, a, highlights[0], 0.3, run)
    _ai(db, a, highlights[1], 0.3, run)
    judgments.judge(db, a, [h[1]], True)
    _ai(db, a, highlights[2], 0.7, run)
    judgments.judge(db, a, [h[2]], False)
    # h[3] has no AI row. h[4] is a random pick the user accepted.
    _ai(db, a, highlights[4], 0.1, run)
    judgments.judge(db, a, [h[4]], True)

    body = client.get(f"/codes/{a}/review").json()
    assert [r["highlight_id"] for r in body["found"]] == [h[1], h[2]]
    assert [r["highlight_id"] for r in body["below_threshold"]] == [h[0], h[3]]
    assert [r["highlight_id"] for r in body["random"]] == [h[4]]
    assert body["found"][1]["final"] is False


def test_review_hides_retired_code_from_other_codes(client, db, codes, highlights):
    a, b, r_code = codes["A"], codes["B"], codes["R"]
    h = highlights[0][0]
    _run_rows(db, a, [(h, "similarity")])
    judgments.judge(db, b, [h], True)
    judgments.judge(db, r_code, [h], True)
    body = client.get(f"/codes/{a}/review").json()
    others = body["below_threshold"][0]["other_codes"]
    assert {o["code_id"] for o in others} == {b, codes["C"]}
    assert next(o for o in others if o["code_id"] == b)["final"] is True


def test_review_without_runs_and_unknown_code(client, codes):
    body = client.get(f"/codes/{codes['C']}/review").json()
    assert (body["run_id"], body["found"], body["below_threshold"], body["random"]) == (None, [], [], [])
    assert client.get("/codes/999999/review").status_code == 404


def test_agreement_route(client, db, codes, highlights):
    a = codes["A"]
    _seed_pairs(db, a, highlights, [(True, True), (False, True)])
    body = client.get(f"/codes/{a}/agreement").json()
    assert (body["n"], body["both_yes"], body["ai_only"]) == (2, 1, 1)
    assert client.get("/codes/999999/agreement").status_code == 404
