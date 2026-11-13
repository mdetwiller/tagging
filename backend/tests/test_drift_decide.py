from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, CodeRevision, CodeRules, DriftProposal
from app.schemas import CodeEdit
from app.services import rules
from app.services.codebook_edit import edit_code
from app.services.export import build_export

client = TestClient(app)

NAME, DEFINITION = "Feeling unheard", "People in power do not listen."
INCLUDE, EXCLUDE = ["Says officials ignore residents"], ["Complaints about neighbours"]
NEW_NAME, NEW_INCLUDE = "Not listened to", ["Names a public official", "Describes being dismissed"]


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture
def setup(db) -> dict:
    """Code (threshold 0.5) with user rules, a sibling named "Taken", and a suggested proposal
    that renames the code, raises the threshold to 0.7 and replaces the include rules."""
    book = Codebook(name="drift decide test")
    db.add(book)
    db.flush()
    code = Code(codebook_id=book.id, name=NAME, definition=DEFINITION, threshold=0.5)
    other = Code(codebook_id=book.id, name="Taken", definition="Another code.")
    db.add_all([code, other])
    db.commit()
    rules.set_rules(db, code.id, INCLUDE, EXCLUDE, source="user")
    proposal = DriftProposal(
        code_id=code.id, status="suggested",
        base={"name": NAME, "definition": DEFINITION, "threshold": 0.5, "include": INCLUDE, "exclude": EXCLUDE},
        proposed={"name": NEW_NAME, "threshold": 0.7, "include": NEW_INCLUDE},
        rationale="Too many false positives.", tokens=1234,
    )
    db.add(proposal)
    db.commit()
    return {"code": code.id, "proposal": proposal.id, "book": book.id}


def _count(db, model, code_id: int) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.code_id == code_id))


def _code(db, code_id: int) -> Code:
    db.expire_all()
    return db.get(Code, code_id)


def _accept(pid: int, parts: list[str], values: dict | None = None):
    body: dict = {"parts": parts}
    if values is not None:
        body["values"] = values
    return client.post(f"/proposals/{pid}/accept", json=body)


def test_accept_threshold_and_rules_changes_only_those(db, setup):
    db.get(Code, setup["code"]).query_stale = False
    db.commit()
    revisions, rule_rows = _count(db, CodeRevision, setup["code"]), _count(db, CodeRules, setup["code"])
    res = _accept(setup["proposal"], ["threshold", "rules"])
    assert res.status_code == 200, res.text
    body = res.json()
    code = _code(db, setup["code"])
    assert code.name == NAME and code.definition == DEFINITION and code.threshold == 0.7
    assert code.query_stale is False
    assert rules.current_rules(db, code.id) == (NEW_INCLUDE, EXCLUDE)
    assert _count(db, CodeRevision, code.id) == revisions + 1
    assert _count(db, CodeRules, code.id) == rule_rows + 1
    row = rules.current_row(db, code.id)
    assert (row.source, row.proposal_id) == ("proposal", setup["proposal"])
    revision = db.scalars(select(CodeRevision).where(CodeRevision.code_id == code.id).order_by(CodeRevision.id.desc())).first()
    assert revision.field == "threshold"
    assert body["status"] == "accepted" and body["decided_at"]
    assert body["applied"] == {
        "threshold": 0.7, "include": NEW_INCLUDE, "exclude": EXCLUDE, "edited": False, "revision_ids": [revision.id],
    }


def test_accept_with_edited_values_applies_and_flags_them(db, setup):
    db.get(Code, setup["code"]).query_stale = False
    db.commit()
    res = _accept(setup["proposal"], ["name", "threshold"], {"threshold": 0.65, "name": "  Brushed off  "})
    assert res.status_code == 200, res.text
    code = _code(db, setup["code"])
    assert (code.name, code.threshold, code.query_stale) == ("Brushed off", 0.65, True)
    applied = res.json()["applied"]
    assert applied["edited"] is True
    assert (applied["name"], applied["threshold"]) == ("Brushed off", 0.65)
    assert len(applied["revision_ids"]) == 2


def test_accept_values_equal_to_proposed_are_not_edited(db, setup):
    res = _accept(setup["proposal"], ["threshold"], {"threshold": 0.7})
    assert res.status_code == 200 and res.json()["applied"]["edited"] is False


def test_accept_may_clear_rules_and_set_unproposed_list(db, setup):
    res = _accept(setup["proposal"], ["rules"], {"include": [], "exclude": ["  New exclusion "]})
    assert res.status_code == 200, res.text
    assert rules.current_rules(db, setup["code"]) == ([], ["New exclusion"])
    assert res.json()["applied"]["edited"] is True


def test_accept_after_manual_rename_is_409_naming_field(db, setup):
    edit_code(db, setup["code"], CodeEdit(name="Renamed by hand"))
    revisions, rule_rows = _count(db, CodeRevision, setup["code"]), _count(db, CodeRules, setup["code"])
    res = _accept(setup["proposal"], ["name", "threshold", "rules"])
    assert res.status_code == 409
    assert "name" in res.json()["detail"]
    code = _code(db, setup["code"])
    assert (code.name, code.threshold) == ("Renamed by hand", 0.5)
    assert _count(db, CodeRevision, setup["code"]) == revisions
    assert _count(db, CodeRules, setup["code"]) == rule_rows
    assert db.get(DriftProposal, setup["proposal"]).status == "suggested"


def test_accept_after_manual_rules_change_is_409_naming_rules(db, setup):
    rules.set_rules(db, setup["code"], INCLUDE, [], source="user")
    res = _accept(setup["proposal"], ["rules"])
    assert res.status_code == 409 and "rules" in res.json()["detail"]


@pytest.mark.parametrize(
    "parts, values",
    [
        ([], None),
        (["definition"], None),  # not proposed and no value given
        (["threshold"], {"threshold": 1.5}),
        (["name"], {"name": "   "}),
        (["name"], {"name": "taken"}),  # duplicates the sibling, case-insensitively
        (["definition"], {"definition": " "}),
        (["rules"], {"include": ["ok", " "]}),
        (["rules"], {"include": [f"rule {i}" for i in range(9)]}),
        (["threshold"], {"name": "Unaccepted edit"}),  # a value for a part not accepted
    ],
)
def test_accept_rejects_invalid_requests_with_400(db, setup, parts, values):
    res = _accept(setup["proposal"], parts, values)
    assert res.status_code == 400, res.text
    assert _code(db, setup["code"]).threshold == 0.5
    assert db.get(DriftProposal, setup["proposal"]).status == "suggested"


def test_accept_while_score_running_is_409(db, setup):
    db.get(DriftProposal, setup["proposal"]).score_status = "running"
    db.commit()
    assert _accept(setup["proposal"], ["threshold"]).status_code == 409


def test_unknown_proposal_is_404():
    assert _accept(999999, ["threshold"]).status_code == 404
    assert client.post("/proposals/999999/dismiss").status_code == 404
    assert client.post("/proposals/999999/rollback").status_code == 404


def test_dismiss_then_accept_is_409(db, setup):
    res = client.post(f"/proposals/{setup['proposal']}/dismiss")
    assert res.status_code == 200
    assert res.json()["status"] == "dismissed" and res.json()["decided_at"]
    assert _accept(setup["proposal"], ["threshold"]).status_code == 409
    assert client.post(f"/proposals/{setup['proposal']}/dismiss").status_code == 409
    assert _code(db, setup["code"]).threshold == 0.5


def test_rollback_restores_base_and_only_adds_rows(db, setup):
    assert _accept(setup["proposal"], ["name", "threshold", "rules"]).status_code == 200
    revisions, rule_rows = _count(db, CodeRevision, setup["code"]), _count(db, CodeRules, setup["code"])
    res = client.post(f"/proposals/{setup['proposal']}/rollback")
    assert res.status_code == 200, res.text
    body = res.json()
    code = _code(db, setup["code"])
    assert (code.name, code.definition, code.threshold) == (NAME, DEFINITION, 0.5)
    assert rules.current_rules(db, code.id) == (INCLUDE, EXCLUDE)
    assert _count(db, CodeRevision, code.id) == revisions + 2
    assert _count(db, CodeRules, code.id) == rule_rows + 1
    row = rules.current_row(db, code.id)
    assert (row.source, row.proposal_id) == ("rollback", setup["proposal"])
    assert body["status"] == "rolled_back"
    assert len(body["applied"]["rollback_revision_ids"]) == 2
    assert len(body["applied"]["revision_ids"]) == 2
    assert client.post(f"/proposals/{setup['proposal']}/rollback").status_code == 409


def test_rollback_after_manual_threshold_edit_is_409(db, setup):
    assert _accept(setup["proposal"], ["threshold", "rules"]).status_code == 200
    edit_code(db, setup["code"], CodeEdit(threshold=0.6))
    rule_rows = _count(db, CodeRules, setup["code"])
    res = client.post(f"/proposals/{setup['proposal']}/rollback")
    assert res.status_code == 409 and "threshold" in res.json()["detail"]
    assert _code(db, setup["code"]).threshold == 0.6
    assert _count(db, CodeRules, setup["code"]) == rule_rows
    assert db.get(DriftProposal, setup["proposal"]).status == "accepted"


def test_rollback_only_the_newest_accepted(db, setup):
    assert _accept(setup["proposal"], ["threshold"]).status_code == 200
    newer = DriftProposal(
        code_id=setup["code"], status="suggested",
        base={"name": NAME, "definition": DEFINITION, "threshold": 0.7, "include": INCLUDE, "exclude": EXCLUDE},
        proposed={"definition": "A sharper definition."},
    )
    db.add(newer)
    db.commit()
    assert client.post(f"/proposals/{newer.id}/rollback").status_code == 409  # still suggested
    assert _accept(newer.id, ["definition"]).status_code == 200
    assert client.post(f"/proposals/{setup['proposal']}/rollback").status_code == 409
    assert client.post(f"/proposals/{newer.id}/rollback").status_code == 200
    # With the newer one rolled back, the older one is the newest accepted again.
    assert client.post(f"/proposals/{setup['proposal']}/rollback").status_code == 200
    assert _code(db, setup["code"]).threshold == 0.5


def test_export_includes_proposals(db, setup):
    assert _accept(setup["proposal"], ["threshold"]).status_code == 200
    with CorticoSession() as db_c:
        exported = build_export(db, db_c)
    proposals = exported["x_tagging"]["codes"][str(setup["code"])]["proposals"]
    assert len(proposals) == 1
    p = proposals[0]
    assert set(p) == {
        "id", "status", "base", "proposed", "applied", "rationale", "score_summary", "tokens", "created_at", "decided_at",
    }
    assert (p["id"], p["status"], p["tokens"], p["rationale"]) == (setup["proposal"], "accepted", 1234, "Too many false positives.")
    assert p["applied"]["threshold"] == 0.7 and p["decided_at"]
