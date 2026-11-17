from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_config
from app.db import Base, CorticoSession, TaggingSession, get_tagging_db
from app.main import app
from app.models import Code, Codebook, CodeExemplar, Coding
from app.models_cortico import Highlight
from app.schemas import CodeEdit
from app.services import judgments
from app.services.codebook_edit import edit_code
from app.services.export import NoCodebook, build_export
from app.services.search_run import code_instructions


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, int]]:
    with CorticoSession() as s:
        return list(s.execute(select(Highlight.id, Highlight.conversation_id).order_by(Highlight.id).limit(10)))


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture
def db_c():
    with CorticoSession() as s:
        yield s


@pytest.fixture
def codes(db) -> dict[str, int]:
    """A new current codebook: parent P with child A, and a retired code R."""
    book = Codebook(name="export test")
    db.add(book)
    db.flush()
    p = Code(codebook_id=book.id, name="P", definition="Parent.")
    db.add(p)
    db.flush()
    a = Code(codebook_id=book.id, name="A", definition="Child.", parent_code_id=p.id, threshold=0.6)
    r = Code(codebook_id=book.id, name="R", definition="Retired.", status="RETIRED")
    db.add_all([a, r])
    db.flush()
    db.add(CodeExemplar(code_id=a.id, text="An exemplar of A."))
    db.commit()
    return {"P": p.id, "A": a.id, "R": r.id}


def _ai(db, code_id: int, highlight: tuple[int, int], p: float) -> None:
    db.add(
        Coding(
            code_id=code_id, highlight_id=highlight[0], conversation_id=highlight[1],
            is_ai_generated=True, ai_probability=p, status="suggested",
            created_by=get_config().user_id, ai_metadata={"model": "test"},
        )
    )
    db.commit()


def _codings(export: dict, code_id: int) -> list[dict]:
    return [c for c in export["codings"] if c["code_id"] == code_id]


def test_export_codes_flat_with_parent_id(db, db_c, codes):
    out = build_export(db, db_c)
    assert out["codebook"]["name"] == "export test"
    by_id = {c["id"]: c for c in out["codebook"]["codes"]}
    assert set(by_id) == set(codes.values())
    assert by_id[codes["A"]] == {
        "id": codes["A"], "name": "A", "description": "Child.", "parent_id": codes["P"],
        "code_type": "THEMATIC", "status": "ACTIVE",
    }
    assert by_id[codes["P"]]["parent_id"] is None
    assert by_id[codes["R"]]["status"] == "RETIRED"
    assert out["x_tagging"]["deviations"] == ["coding.highlight_id stored directly; highlight_share_id is null"]


def test_export_keeps_every_coding(db, db_c, codes, highlights):
    a, r = codes["A"], codes["R"]
    judgments.judge(db, a, [highlights[0][0]], True, db_c)
    judgments.judge(db, a, [highlights[0][0]], False, db_c)
    _ai(db, a, highlights[0], 0.9)
    _ai(db, r, highlights[1], 0.2)
    out = build_export(db, db_c)
    rows = _codings(out, a)
    assert len(rows) == 3
    assert [r["is_ai_generated"] for r in rows] == [False, False, True]
    first = rows[0]
    assert first["highlight_id"] == highlights[0][0]
    assert first["conversation_id"] == highlights[0][1]
    assert first["highlight_share_id"] is None
    assert first["status"] == "applied"
    assert first["created_by"] == get_config().user_id
    assert rows[2]["ai_probability"] == 0.9
    assert rows[2]["ai_metadata"] == {"model": "test"}
    assert len(_codings(out, r)) == 1
    other_codes = {c["code_id"] for c in out["codings"]} - set(codes.values())
    assert not other_codes


def test_export_final_only_on_latest(db, db_c, codes, highlights):
    a = codes["A"]
    h0, h1 = highlights[0], highlights[1]
    # h0: human rows exist, so only the latest human row carries final.
    _ai(db, a, h0, 0.9)
    judgments.judge(db, a, [h0[0]], True, db_c)
    judgments.judge(db, a, [h0[0]], False, db_c)
    _ai(db, a, h0, 0.95)
    # h1: AI only, so the latest AI row carries final against the 0.6 threshold.
    _ai(db, a, h1, 0.9)
    _ai(db, a, h1, 0.5)
    out = build_export(db, db_c)
    h0_rows = [r for r in _codings(out, a) if r["highlight_id"] == h0[0]]
    assert [r["final"] for r in h0_rows] == [None, None, False, None]
    assert h0_rows[2]["final"] == judgments.final_for(db, a, h0[0])
    h1_rows = [r for r in _codings(out, a) if r["highlight_id"] == h1[0]]
    assert [r["final"] for r in h1_rows] == [None, False]
    assert h1_rows[1]["final"] == judgments.final_for(db, a, h1[0])


def test_export_includes_revisions_and_agreement(db, db_c, codes, highlights):
    a = codes["A"]
    edit_code(db, a, CodeEdit(name="A2"))
    edit_code(db, a, CodeEdit(threshold=0.7))
    judgments.judge(db, a, [highlights[0][0]], True, db_c)
    _ai(db, a, highlights[0], 0.8)
    out = build_export(db, db_c)
    x = out["x_tagging"]["codes"][str(a)]
    assert [(r["field"], r["before"], r["after"]) for r in x["revisions"]] == [
        ("name", "A", "A2"), ("threshold", 0.6, 0.7),
    ]
    assert all(isinstance(r["created_at"], str) for r in x["revisions"])
    assert x["threshold"] == 0.7
    assert x["agreement"] == judgments.agreement(db, a)
    assert x["agreement"]["both_yes"] == 1
    code = db.get(Code, a)
    texts = {highlights[0][0]: db_c.get(Highlight, highlights[0][0]).text}
    assert x["instructions"] == code_instructions(db, code, texts)
    assert "An exemplar of A." in x["instructions"]
    assert set(out["x_tagging"]["codes"]) == {str(c) for c in codes.values()}


def test_export_is_json_serialisable(db, db_c, codes, highlights):
    judgments.judge(db, codes["A"], [highlights[0][0]], True, db_c)
    out = build_export(db, db_c)
    assert json.loads(json.dumps(out)) == out
    assert isinstance(out["exported_at"], str)
    assert all(isinstance(c["created_at"], str) for c in out["codings"])


def test_export_counts_earlier_codebooks(db, db_c, highlights):
    old = Codebook(name="older book")
    db.add(old)
    db.flush()
    old_code = Code(codebook_id=old.id, name="Old", definition="Old code.")
    db.add(old_code)
    db.commit()
    judgments.judge(db, old_code.id, [highlights[0][0], highlights[1][0]], True)
    _ai(db, old_code.id, highlights[2], 0.9)
    new = Codebook(name="newer book")
    db.add(new)
    db.flush()
    db.add(Code(codebook_id=new.id, name="New", definition="New code."))
    db.commit()

    earlier = build_export(db, db_c)["x_tagging"]["earlier_codebooks"]
    assert {"id": old.id, "name": "older book", "codings": 3} in earlier
    assert all(e["id"] < new.id for e in earlier)
    assert [e["id"] for e in earlier] == sorted(e["id"] for e in earlier)


def test_export_route_download(codes):
    with TestClient(app) as client:
        r = client.get("/export")
    assert r.status_code == 200
    expected = f'attachment; filename="tagging-export-{date.today().isoformat()}.json"'
    assert r.headers["content-disposition"] == expected
    assert r.json()["codebook"]["name"] == "export test"


def test_export_without_codebook_404(db_c):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as empty:
        with pytest.raises(NoCodebook):
            build_export(empty, db_c)

        app.dependency_overrides[get_tagging_db] = lambda: empty
        try:
            with TestClient(app) as client:
                r = client.get("/export")
        finally:
            app.dependency_overrides.pop(get_tagging_db)
    assert r.status_code == 404
