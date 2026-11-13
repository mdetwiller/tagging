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
    """A finished find-more run for the code with these (highlight_id, picked_by) rows."""
    run = SearchRun(scope_code_id=code_id, status="done", summary={"kind": "find_more"})
    db.add(run)
    db.flush()
    for position, (hid, picked_by) in enumerate(rows, start=1):
        db.add(RunHighlight(run_id=run.id, target_code_id=code_id, highlight_id=hid, picked_by=picked_by, position=position))
    db.commit()
    return run.id


def _sample_run(db, judged: list[tuple[tuple[int, int], dict[int, float]]]) -> int:
    """A finished sample run that judged each highlight, with AI rows {code_id: p}."""
    codes = {c for _, ps in judged for c in ps}
    run = SearchRun(
        scope_code_id=None, status="done",
        summary={"kind": "sample", "codes": {str(c): {"positives": 0, "tries": 1, "few": True} for c in codes}},
    )
    db.add(run)
    db.commit()
    for highlight, ps in judged:
        for code_id, p in ps.items():
            _ai(db, code_id, highlight, p, run.id)
    return run.id


def _ids(rows: list[dict]) -> list[int]:
    return [r["highlight_id"] for r in rows]


def test_review_combines_sample_and_find_more_runs(client, db, codes, highlights):
    a, b = codes["A"], codes["B"]
    h = [x[0] for x in highlights]
    hl = highlights
    # h2 got no answer for A, only for B.
    sample = _sample_run(db, [(hl[0], {a: 0.9, b: 0.1}), (hl[1], {a: 0.2, b: 0.1}), (hl[2], {b: 0.5})])
    older = _run_rows(db, a, [(h[3], "similarity"), (h[4], "random"), (h[0], "similarity")])
    _ai(db, a, hl[3], 0.8, older)
    _ai(db, a, hl[4], 0.1, older)
    newer = _run_rows(db, a, [(h[5], "similarity"), (h[6], "similarity"), (h[1], "random")])
    _ai(db, a, hl[5], 0.1, newer)
    _ai(db, a, hl[6], 0.95, newer)
    # A final yes from no run is listed under found too.
    judgments.judge(db, a, [h[15]], True)

    body = client.get(f"/codes/{a}/review").json()
    assert body["code"]["id"] == a
    assert "run_id" not in body
    assert body["sample_run_id"] == sample
    assert body["find_more_run_ids"] == [newer, older]
    assert _ids(body["found"]) == [h[0], h[6], h[3], h[15]]
    assert [r["source"] for r in body["found"]] == ["sample", "search", "search", None]
    # h2 has no answer for A, so it is not in A's Random section.
    assert _ids(body["random"]) == [h[1], h[4]]
    assert [r["source"] for r in body["random"]] == ["sample", "random"]
    assert _ids(body["below_threshold"]) == [h[5]]
    assert body["below_threshold"][0]["source"] == "search"
    listed = _ids(body["found"]) + _ids(body["random"]) + _ids(body["below_threshold"])
    assert len(listed) == len(set(listed))

    first = body["found"][0]
    with CorticoSession() as s:
        row = s.get(Highlight, h[0])
        project_id = s.get(Conversation, row.conversation_id).project_id
        text = row.text
    assert first == {
        "highlight_id": h[0], "conversation_id": row.conversation_id, "project_id": project_id,
        "text": text, "ai_probability": 0.9, "human": None, "final": True,
        "status": first["status"], "source": "sample",
        "other_codes": [{"code_id": b, "name": "B", "final": False}, {"code_id": codes["C"], "name": "C", "final": None}],
    }
    assert first["status"] in ("not_started", "in_progress", "completed")


def test_review_uses_only_latest_sample_run(client, db, codes, highlights):
    a = codes["A"]
    h = [x[0] for x in highlights]
    hl = highlights
    _sample_run(db, [(hl[10], {a: 0.1}), (hl[11], {a: 0.8})])
    latest = _sample_run(db, [(hl[20], {a: 0.1})])
    # A sample run that failed before its first answer lists no codes and is skipped.
    db.add(SearchRun(scope_code_id=None, status="failed", summary={"kind": "sample", "codes": {}, "error": "boom"}))
    db.commit()

    body = client.get(f"/codes/{a}/review").json()
    assert body["sample_run_id"] == latest
    assert _ids(body["random"]) == [h[20]]
    # The older sample's h11 is still a final yes (AI 0.8), so it is listed, with no source.
    assert _ids(body["found"]) == [h[11]]
    assert body["found"][0]["source"] is None
    assert h[10] not in _ids(body["found"]) + _ids(body["random"]) + _ids(body["below_threshold"])


def test_review_random_pick_accepted_moves_to_found(client, db, codes, highlights):
    a = codes["A"]
    h = [x[0] for x in highlights]
    run = _run_rows(db, a, [(h[0], "similarity"), (h[1], "random")])
    _ai(db, a, highlights[1], 0.1, run)
    judgments.judge(db, a, [h[1]], True)
    body = client.get(f"/codes/{a}/review").json()
    assert _ids(body["found"]) == [h[1]]
    assert body["found"][0]["source"] == "random"
    assert body["random"] == []
    assert _ids(body["below_threshold"]) == [h[0]]


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
    # h[3] has no AI row. h[4] is a random pick below the threshold.
    _ai(db, a, highlights[4], 0.1, run)

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
    assert (body["sample_run_id"], body["find_more_run_ids"]) == (None, [])
    assert (body["found"], body["below_threshold"], body["random"]) == ([], [], [])
    assert client.get("/codes/999999/review").status_code == 404


def test_agreement_route(client, db, codes, highlights):
    a = codes["A"]
    _seed_pairs(db, a, highlights, [(True, True), (False, True)])
    body = client.get(f"/codes/{a}/agreement").json()
    assert (body["n"], body["both_yes"], body["ai_only"]) == (2, 1, 1)
    assert client.get("/codes/999999/agreement").status_code == 404
