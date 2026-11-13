from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import TAGGING_DIR
from app.db import TaggingSession
from app.main import app
from app.models import Code, CodeExemplar, Coding
from app.services.codebook_import import import_reconciled

FIXTURE = Path(__file__).parent / "fixtures" / "reconciled_small.json"
REAL_FILE = TAGGING_DIR.parent / "Codestormer" / "data" / "reconciled" / "reconciled_codebook.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def db():
    session = TaggingSession()
    try:
        yield session
    finally:
        session.close()


def _codes(db, codebook_id: int) -> list[Code]:
    return list(db.scalars(select(Code).where(Code.codebook_id == codebook_id).order_by(Code.id)))


def test_import_counts(db):
    result = import_reconciled(db, _fixture(), "reconciled_small.json")
    assert result["parents"] == 2
    assert result["children"] == 3
    assert result["exemplars"] == 2
    assert isinstance(result["codebook_id"], int)


def test_import_flags_empty_definition(db):
    result = import_reconciled(db, _fixture(), "reconciled_small.json")
    assert any("Data Centers in Community" in w for w in result["warnings"])
    assert not any("Digital Disconnection" in w for w in result["warnings"])


def test_import_parent_links(db):
    data = _fixture()
    result = import_reconciled(db, data, "reconciled_small.json")
    codes = _codes(db, result["codebook_id"])
    by_origin = {c.origin_id: c for c in codes}
    assert len(codes) == 5
    for p_order, parent in enumerate(data["codes"]):
        parent_row = by_origin[parent["id"]]
        assert parent_row.parent_code_id is None
        assert parent_row.display_order == p_order
        assert parent_row.query_stale is True
        for c_order, child in enumerate(parent["children"]):
            child_row = by_origin[child["id"]]
            assert child_row.parent_code_id == parent_row.id
            assert child_row.display_order == c_order
            assert child_row.query_stale is True
    merged = by_origin["rc-child-a1"].x_source
    assert merged["source"] == "reconciled"
    assert merged["merged_from"] == ["Echo Chambering"]
    assert by_origin["rc-parent-a"].x_source["rq_provenance"] == {
        "id": 12,
        "name": "How do people talk about technology?",
    }


def test_exemplar_text_prefers_span(db):
    result = import_reconciled(db, _fixture(), "reconciled_small.json")
    by_origin = {c.origin_id: c for c in _codes(db, result["codebook_id"])}

    def exemplars(origin: str) -> list[CodeExemplar]:
        return list(
            db.scalars(select(CodeExemplar).where(CodeExemplar.code_id == by_origin[origin].id))
        )

    with_span = exemplars("rc-child-a1")
    assert [(e.text, e.snippet_id, e.conversation_id) for e in with_span] == [
        ("Well, during the", 27824, 132)
    ]
    text_only = exemplars("rc-child-b1")
    assert [e.text for e in text_only] == ["People just stopped talking to each other."]
    # merged_from copies and highlights are never imported as exemplars.
    all_texts = {
        e.text
        for code in by_origin.values()
        for e in db.scalars(select(CodeExemplar).where(CodeExemplar.code_id == code.id))
    }
    assert "must not be imported" not in all_texts and "must" not in all_texts
    assert "highlight text" not in all_texts


def test_import_rejects_cortico_format():
    body = {"codes": [{"id": 1, "name": "A", "parent_id": None}, {"id": 2, "name": "B", "parent_id": 1}]}
    with TestClient(app) as client:
        resp = client.post(
            "/codebooks/import",
            files={"file": ("cortico.json", json.dumps(body), "application/json")},
        )
    assert resp.status_code == 400
    assert "Cortico-format export" in resp.json()["detail"]


def test_import_rejects_invalid_json():
    with TestClient(app) as client:
        resp = client.post(
            "/codebooks/import", files={"file": ("bad.json", "{not json", "application/json")}
        )
    assert resp.status_code == 400


def test_reimport_creates_new_codebook():
    raw = FIXTURE.read_bytes()
    with TestClient(app) as client:
        first = client.post("/codebooks/import", files={"file": ("r.json", raw, "application/json")})
        second = client.post("/codebooks/import", files={"file": ("r.json", raw, "application/json")})
        assert first.status_code == 200 and second.status_code == 200
        assert second.json()["codebook_id"] != first.json()["codebook_id"]
        current = client.get("/codebooks/current")
    assert current.status_code == 200
    body = current.json()
    assert body["id"] == second.json()["codebook_id"]
    assert [c["name"] for c in body["codes"]] == ["Technology and Social Connection", "Civic Life"]
    tech = body["codes"][0]
    assert set(tech) == {
        "id", "name", "definition", "parent_code_id", "status", "threshold",
        "display_order", "exemplar_count", "children",
    }
    assert body["human_judgments"] == 0
    assert [c["name"] for c in tech["children"]] == ["Digital Disconnection", "Data Centers in Community"]
    assert tech["children"][0]["exemplar_count"] == 1
    assert tech["children"][0]["parent_code_id"] == tech["id"]
    assert tech["children"][0]["children"] == []


def test_current_counts_human_judgments(db):
    raw = FIXTURE.read_bytes()
    with TestClient(app) as client:
        book_id = client.post("/codebooks/import", files={"file": ("r.json", raw, "application/json")}).json()[
            "codebook_id"
        ]
        code_id = _codes(db, book_id)[0].id
        for is_ai, status in ((False, "applied"), (False, "not_applied"), (True, "suggested")):
            db.add(Coding(code_id=code_id, highlight_id=1, conversation_id=1, status=status,
                          is_ai_generated=is_ai, ai_probability=0.5 if is_ai else None, created_by=101))
        db.commit()
        assert client.get("/codebooks/current").json()["human_judgments"] == 2
        client.post("/codebooks/import", files={"file": ("r.json", raw, "application/json")})
        assert client.get("/codebooks/current").json()["human_judgments"] == 0


@pytest.mark.skipif(not REAL_FILE.is_file(), reason="Codestormer reconciled codebook not present")
def test_import_real_file(db):
    data = json.loads(REAL_FILE.read_text())
    result = import_reconciled(db, data, REAL_FILE.name)
    assert result["parents"] == 5
    assert result["children"] == 52
