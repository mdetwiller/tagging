from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import TaggingSession
from app.main import app
from app.models import Code, Codebook, CodebookVersion, CodeRevision, Coding
from app.services import rules


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def book() -> dict[str, int]:
    """A new current codebook: parent P with child C, and top-level Q."""
    with TaggingSession() as s:
        cb = Codebook(name="versions test")
        s.add(cb)
        s.flush()
        p = Code(codebook_id=cb.id, name="P", definition="Def P.", display_order=1, query_stale=False)
        q = Code(codebook_id=cb.id, name="Q", definition="Def Q.", display_order=3, query_stale=False)
        s.add_all([p, q])
        s.flush()
        c = Code(codebook_id=cb.id, parent_code_id=p.id, name="C", definition="Def C.", display_order=2, query_stale=False)
        s.add(c)
        s.commit()
        return {"book": cb.id, "P": p.id, "Q": q.id, "C": c.id}


def _code(code_id: int) -> Code:
    with TaggingSession() as s:
        code = s.get(Code, code_id)
        s.expunge(code)
        return code


def test_add_parent_and_child_codes(client, book):
    res = client.post("/codes", json={"name": "  New parent ", "definition": "D."})
    assert res.status_code == 201
    new_parent = res.json()
    assert new_parent["name"] == "New parent" and new_parent["parent_code_id"] is None
    assert new_parent["status"] == "ACTIVE" and new_parent["threshold"] == 0.5
    res = client.post("/codes", json={"name": "New child", "parent_code_id": new_parent["id"]})
    assert res.status_code == 201 and res.json()["parent_code_id"] == new_parent["id"]
    code = _code(res.json()["id"])
    assert code.codebook_id == book["book"] and code.query_stale and code.source == "user"
    tree = client.get("/codebooks/current").json()["codes"]
    assert tree[-1]["id"] == new_parent["id"] and [c["name"] for c in tree[-1]["children"]] == ["New child"]
    with TaggingSession() as s:
        rev = s.scalars(select(CodeRevision).where(CodeRevision.code_id == code.id)).one()
        assert rev.field == "created" and rev.after["name"] == "New child"


def test_add_code_rejects_bad_input(client, book):
    assert client.post("/codes", json={"name": "  "}).status_code == 400
    assert client.post("/codes", json={"name": "X", "parent_code_id": book["C"]}).status_code == 400
    assert client.post("/codes", json={"name": "X", "parent_code_id": 999999}).status_code == 400


def test_save_list_and_restore(client, book):
    rules.set_rules(TaggingSession(), book["P"], ["inc"], [], source="user")
    res = client.post("/codebook_versions", json={"name": "baseline"})
    assert res.status_code == 201 and res.json()["code_count"] == 3
    vid = res.json()["id"]

    with TaggingSession() as s:
        s.add(Coding(code_id=book["C"], highlight_id=1, conversation_id=1, status="applied",
                     is_ai_generated=False, created_by=101))
        s.commit()
    client.patch(f"/codes/{book['P']}", json={"name": "P renamed", "threshold": 0.7})
    client.patch(f"/codes/{book['C']}", json={"parent_code_id": None})
    client.patch(f"/codes/{book['Q']}", json={"status": "RETIRED"})
    added = client.post("/codes", json={"name": "Added"}).json()["id"]
    rules.set_rules(TaggingSession(), book["P"], ["changed"], ["x"], source="user")

    diff = {d["code_id"]: d for d in client.get(f"/codebook_versions/{vid}/diff").json()}
    assert diff[added]["kind"] == "retire_added"
    assert {c["field"] for c in diff[book["P"]]["changes"]} == {"name", "threshold", "rules"}
    assert diff[book["C"]]["changes"] == [{"field": "parent_code_id", "before": None, "after": book["P"]}]
    assert diff[book["Q"]]["changes"] == [{"field": "status", "before": "RETIRED", "after": "ACTIVE"}]

    res = client.post(f"/codebook_versions/{vid}/restore")
    assert res.status_code == 200 and res.json()["changed_codes"] == 4
    p, c, q, a = (_code(i) for i in (book["P"], book["C"], book["Q"], added))
    assert (p.name, p.threshold, p.query_stale) == ("P", 0.5, True)
    assert c.parent_code_id == book["P"] and q.status == "ACTIVE" and a.status == "RETIRED"
    with TaggingSession() as s:
        assert rules.current_rules(s, book["P"]) == (["inc"], [])
        assert rules.current_row(s, book["P"]).source == "restore"
        # Judgments are untouched.
        assert s.scalar(select(func.count(Coding.id)).where(Coding.code_id == book["C"])) == 1
        # Each restored field is a revision.
        fields = list(s.scalars(select(CodeRevision.field).where(CodeRevision.code_id == book["P"]).order_by(CodeRevision.id)))
        assert fields[-2:] == ["name", "threshold"]
    assert client.get(f"/codebook_versions/{vid}/diff").json() == []

    listed = client.get("/codebook_versions").json()
    assert [v["source"] for v in listed[:2]] == ["before_restore", "user"]
    before_id = res.json()["saved_version_id"]
    assert listed[0]["id"] == before_id and listed[0]["restored_version_id"] == vid
    assert listed[0]["name"] == 'Before restoring "baseline"'

    # Restoring the automatic save undoes the restore.
    client.post(f"/codebook_versions/{before_id}/restore")
    p, a = _code(book["P"]), _code(added)
    assert (p.name, p.threshold, a.status) == ("P renamed", 0.7, "ACTIVE")
    with TaggingSession() as s:
        assert rules.current_rules(s, book["P"]) == (["changed"], ["x"])


def test_version_errors(client, book):
    assert client.post("/codebook_versions", json={"name": " "}).status_code == 400
    assert client.get("/codebook_versions/999999/diff").status_code == 404
    assert client.post("/codebook_versions/999999/restore").status_code == 404
    vid = client.post("/codebook_versions", json={"name": "old book"}).json()["id"]
    with TaggingSession() as s:
        s.add(Codebook(name="newer"))
        s.commit()
    assert client.post(f"/codebook_versions/{vid}/restore").status_code == 409
    assert client.get("/codebook_versions").json() == []
    with TaggingSession() as s:
        assert s.get(CodebookVersion, vid) is not None
