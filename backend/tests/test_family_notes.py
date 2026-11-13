from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, FamilyNote
from app.services.export import build_export


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def codes() -> dict[str, int]:
    """A new current codebook: parents P (with child A) and Q."""
    with TaggingSession() as db:
        book = Codebook(name="notes test")
        db.add(book)
        db.flush()
        p = Code(codebook_id=book.id, name="P", definition="Parent.")
        q = Code(codebook_id=book.id, name="Q", definition="Other parent.")
        db.add_all([p, q])
        db.flush()
        a = Code(codebook_id=book.id, name="A", definition="Child.", parent_code_id=p.id)
        db.add(a)
        db.commit()
        return {"P": p.id, "Q": q.id, "A": a.id}


def _rows(code_id: int) -> list[FamilyNote]:
    with TaggingSession() as db:
        rows = list(db.scalars(select(FamilyNote).where(FamilyNote.code_id == code_id).order_by(FamilyNote.id)))
        for r in rows:
            db.expunge(r)
        return rows


def test_get_empty_note(client, codes):
    r = client.get(f"/codes/{codes['P']}/note")
    assert r.status_code == 200
    assert r.json() == {"code_id": codes["P"], "text": "", "updated_at": None}


def test_put_then_get(client, codes):
    r = client.put(f"/codes/{codes['P']}/note", json={"text": "Maybe: caregiving burden"})
    assert r.status_code == 200
    body = r.json()
    assert body["code_id"] == codes["P"]
    assert body["text"] == "Maybe: caregiving burden"
    assert body["updated_at"] is not None
    assert client.get(f"/codes/{codes['P']}/note").json() == body
    rows = _rows(codes["P"])
    assert len(rows) == 1 and rows[0].created_by == get_config().user_id


def test_unchanged_put_writes_no_row(client, codes):
    first = client.put(f"/codes/{codes['P']}/note", json={"text": "same"}).json()
    second = client.put(f"/codes/{codes['P']}/note", json={"text": "same"}).json()
    assert second == first
    assert len(_rows(codes["P"])) == 1
    # An empty put on a code with no note writes nothing either.
    assert client.put(f"/codes/{codes['Q']}/note", json={"text": ""}).json()["updated_at"] is None
    assert _rows(codes["Q"]) == []


def test_history_kept_latest_wins(client, codes):
    client.put(f"/codes/{codes['P']}/note", json={"text": "first"})
    client.put(f"/codes/{codes['P']}/note", json={"text": "second"})
    assert [r.text for r in _rows(codes["P"])] == ["first", "second"]
    assert client.get(f"/codes/{codes['P']}/note").json()["text"] == "second"


def test_child_code_400(client, codes):
    for r in (
        client.get(f"/codes/{codes['A']}/note"),
        client.put(f"/codes/{codes['A']}/note", json={"text": "x"}),
    ):
        assert r.status_code == 400
        assert r.json()["detail"] == "Notes belong to a code family (a top-level code)"
    assert _rows(codes["A"]) == []


def test_unknown_code_404(client, codes):
    assert client.get("/codes/999999/note").status_code == 404
    assert client.put("/codes/999999/note", json={"text": "x"}).status_code == 404


def test_too_long_note_422(client, codes):
    assert client.put(f"/codes/{codes['P']}/note", json={"text": "x" * 20001}).status_code == 422


def test_codebook_flags_parents_with_notes(client, codes):
    client.put(f"/codes/{codes['P']}/note", json={"text": "something"})
    client.put(f"/codes/{codes['Q']}/note", json={"text": "was here"})
    client.put(f"/codes/{codes['Q']}/note", json={"text": "  "})
    body = client.get("/codebooks/current").json()
    flags = {c["id"]: c["has_note"] for c in body["codes"]}
    assert flags == {codes["P"]: True, codes["Q"]: False}
    assert body["codes"][0]["children"][0]["has_note"] is False


def test_export_includes_family_notes(client, codes):
    client.put(f"/codes/{codes['P']}/note", json={"text": "old"})
    saved = client.put(f"/codes/{codes['P']}/note", json={"text": "Missing: transport access"}).json()
    client.put(f"/codes/{codes['Q']}/note", json={"text": ""})
    with TaggingSession() as db, CorticoSession() as db_c:
        notes = build_export(db, db_c)["x_tagging"]["family_notes"]
    assert notes == {
        str(codes["P"]): {"name": "P", "text": "Missing: transport access", "updated_at": saved["updated_at"]}
    }
