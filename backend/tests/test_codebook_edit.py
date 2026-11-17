from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import TaggingSession
from app.main import app
from app.models import Code, CodeRevision, Coding
from app.services.codebook_import import import_reconciled

FIXTURE = Path(__file__).parent / "fixtures" / "reconciled_small.json"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def codes() -> dict[str, int]:
    """Imports the fixture codebook and returns code ids keyed by origin id."""
    session = TaggingSession()
    try:
        result = import_reconciled(session, json.loads(FIXTURE.read_text()), "reconciled_small.json")
        rows = session.scalars(select(Code).where(Code.codebook_id == result["codebook_id"]))
        return {c.origin_id: c.id for c in rows}
    finally:
        session.close()


def _code(code_id: int) -> Code:
    with TaggingSession() as s:
        code = s.get(Code, code_id)
        s.expunge(code)
        return code


def _revisions(code_id: int) -> list[CodeRevision]:
    with TaggingSession() as s:
        rows = list(
            s.scalars(
                select(CodeRevision).where(CodeRevision.code_id == code_id).order_by(CodeRevision.id)
            )
        )
        for r in rows:
            s.expunge(r)
        return rows


def test_rename_writes_revision_first(client, codes):
    cid = codes["rc-child-a1"]
    resp = client.patch(f"/codes/{cid}", json={"name": "Digital Distance"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Digital Distance"
    revs = _revisions(cid)
    assert len(revs) == 1
    assert (revs[0].field, revs[0].before, revs[0].after) == (
        "name",
        "Digital Disconnection",
        "Digital Distance",
    )
    assert revs[0].created_by == 101
    listed = client.get(f"/codes/{cid}/revisions").json()
    assert [(r["field"], r["before"], r["after"]) for r in listed] == [
        ("name", "Digital Disconnection", "Digital Distance")
    ]
    assert listed[0]["created_at"]


def test_definition_edit_marks_query_stale(client, codes):
    cid = codes["rc-child-a1"]
    with TaggingSession() as s:
        s.get(Code, cid).query_stale = False
        s.commit()
    assert client.patch(f"/codes/{cid}", json={"threshold": 0.7}).status_code == 200
    assert _code(cid).query_stale is False
    assert client.patch(f"/codes/{cid}", json={"definition": "New definition."}).status_code == 200
    assert _code(cid).query_stale is True
    with TaggingSession() as s:
        s.get(Code, cid).query_stale = False
        s.commit()
    assert client.patch(f"/codes/{cid}", json={"name": "Renamed"}).status_code == 200
    assert _code(cid).query_stale is True


def test_move_child_to_other_parent(client, codes):
    cid, new_parent = codes["rc-child-a1"], codes["rc-parent-b"]
    old_parent = codes["rc-parent-a"]
    resp = client.patch(f"/codes/{cid}", json={"parent_code_id": new_parent})
    assert resp.status_code == 200
    assert resp.json()["parent_code_id"] == new_parent
    assert _code(cid).parent_code_id == new_parent
    revs = _revisions(cid)
    assert [(r.field, r.before, r.after) for r in revs] == [
        ("parent_code_id", old_parent, new_parent)
    ]


def test_move_rejects_third_level(client, codes):
    # Under a child: would make a third level.
    resp = client.patch(
        f"/codes/{codes['rc-child-a2']}", json={"parent_code_id": codes["rc-child-b1"]}
    )
    assert resp.status_code == 400
    # A parent with children under another parent.
    resp = client.patch(
        f"/codes/{codes['rc-parent-a']}", json={"parent_code_id": codes["rc-parent-b"]}
    )
    assert resp.status_code == 400
    # Its own parent.
    resp = client.patch(
        f"/codes/{codes['rc-parent-a']}", json={"parent_code_id": codes["rc-parent-a"]}
    )
    assert resp.status_code == 400
    assert _revisions(codes["rc-child-a2"]) == []
    assert _revisions(codes["rc-parent-a"]) == []


def test_move_rejects_other_codebook(client, codes):
    other = {}
    with TaggingSession() as s:
        result = import_reconciled(s, json.loads(FIXTURE.read_text()), "again.json")
        other = {c.origin_id: c.id for c in s.scalars(select(Code).where(Code.codebook_id == result["codebook_id"]))}
    resp = client.patch(
        f"/codes/{codes['rc-child-a1']}", json={"parent_code_id": other["rc-parent-b"]}
    )
    assert resp.status_code == 400


def test_move_to_top_level(client, codes):
    cid = codes["rc-child-a1"]
    resp = client.patch(f"/codes/{cid}", json={"parent_code_id": None})
    assert resp.status_code == 200
    assert resp.json()["parent_code_id"] is None
    assert _code(cid).parent_code_id is None
    assert [(r.field, r.before, r.after) for r in _revisions(cid)] == [
        ("parent_code_id", codes["rc-parent-a"], None)
    ]


def test_blank_name_rejected(client, codes):
    cid = codes["rc-child-a1"]
    for name in ("", "   ", "\t\n"):
        resp = client.patch(f"/codes/{cid}", json={"name": name})
        assert resp.status_code == 400
        assert "blank" in resp.json()["detail"]
    assert _code(cid).name == "Digital Disconnection"
    assert _revisions(cid) == []


def test_threshold_bounds(client, codes):
    cid = codes["rc-child-a1"]
    assert client.patch(f"/codes/{cid}", json={"threshold": 1.2}).status_code == 422
    assert client.patch(f"/codes/{cid}", json={"threshold": -0.1}).status_code == 422
    assert client.patch(f"/codes/{cid}", json={"status": "DELETED"}).status_code == 422
    assert client.patch(f"/codes/{cid}", json={"threshold": 1.0}).status_code == 200


def test_retire_keeps_codings(client, codes):
    cid = codes["rc-child-a1"]
    with TaggingSession() as s:
        s.add(Coding(code_id=cid, highlight_id=1, conversation_id=132, status="applied", created_by=101))
        s.commit()
    resp = client.patch(f"/codes/{cid}", json={"status": "RETIRED"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "RETIRED"
    with TaggingSession() as s:
        assert s.get(Code, cid).status == "RETIRED"
        assert len(list(s.scalars(select(Coding).where(Coding.code_id == cid)))) == 1
    assert [(r.field, r.before, r.after) for r in _revisions(cid)] == [
        ("status", "ACTIVE", "RETIRED")
    ]


def test_noop_edit_writes_no_revision(client, codes):
    cid = codes["rc-child-a1"]
    current = _code(cid)
    resp = client.patch(
        f"/codes/{cid}",
        json={
            "name": current.name,
            "definition": current.definition,
            "threshold": current.threshold,
            "parent_code_id": current.parent_code_id,
            "status": current.status,
        },
    )
    assert resp.status_code == 200
    assert client.patch(f"/codes/{cid}", json={}).status_code == 200
    assert _revisions(cid) == []


def test_unknown_code_404(client):
    assert client.patch("/codes/999999", json={"name": "x"}).status_code == 404
    assert client.get("/codes/999999/revisions").status_code == 404
