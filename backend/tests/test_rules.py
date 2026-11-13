from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import CorticoSession, TaggingSession
from app.models import Code, Codebook, CodeExemplar, CodeRules
from app.models_cortico import Highlight
from app.services import judgments, rules
from app.services.export import build_export
from app.services.jev import build_instructions
from app.services.search_run import code_instructions


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, str]]:
    with CorticoSession() as s:
        return list(s.execute(select(Highlight.id, Highlight.text).order_by(Highlight.id).limit(4)))


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture
def db_c():
    with CorticoSession() as s:
        yield s


@pytest.fixture
def code(db) -> Code:
    book = Codebook(name="rules test")
    db.add(book)
    db.flush()
    c = Code(codebook_id=book.id, name="Feeling unheard", definition="People in power do not listen.")
    db.add(c)
    db.flush()
    db.add(CodeExemplar(code_id=c.id, text="Nobody at city hall listens to us."))
    db.commit()
    return c


def test_current_rules_empty_without_row(db, code):
    assert rules.current_rules(db, code.id) == ([], [])


def test_current_rules_is_newest_row(db, code):
    first = rules.set_rules(db, code.id, ["Names an official"], [], "user")
    second = rules.set_rules(db, code.id, ["Names an office"], ["Only about family"], "proposal", proposal_id=7)
    assert second.id > first.id
    assert rules.current_rules(db, code.id) == (["Names an office"], ["Only about family"])
    rows = list(db.scalars(select(CodeRules).where(CodeRules.code_id == code.id).order_by(CodeRules.id)))
    assert [(r.source, r.proposal_id) for r in rows] == [("user", None), ("proposal", 7)]
    assert all(r.created_at is not None for r in rows)


def test_build_instructions_with_rules(code):
    text = build_instructions(code, [], [], [], include=["Names an official"], exclude=["Only about family"])
    assert "Definition: People in power do not listen.\nCounts when:\n- Names an official" in text
    assert "Does not count when:\n- Only about family" in text
    only_include = build_instructions(code, [], [], [], include=["Names an official"])
    assert "Does not count when:" not in only_include


def test_build_instructions_without_rules_is_unchanged(db, code):
    exemplars = list(db.scalars(select(CodeExemplar).where(CodeExemplar.code_id == code.id)))
    expected = (
        'Does this highlight fit the code "Feeling unheard"?\n'
        "Definition: People in power do not listen.\n"
        "Examples that fit:\n"
        "- Nobody at city hall listens to us.\n"
        "- yes text\n"
        "Examples that do not fit:\n"
        "- no text"
    )
    assert build_instructions(code, exemplars, ["yes text"], ["no text"]) == expected
    assert build_instructions(code, exemplars, ["yes text"], ["no text"], include=(), exclude=()) == expected


def test_build_instructions_name_and_definition_override(code):
    text = build_instructions(code, [], [], [], name="Ignored by power", definition="New definition.")
    assert text == 'Does this highlight fit the code "Ignored by power"?\nDefinition: New definition.'


def test_code_instructions_uses_current_rules_and_override(db, code):
    rules.set_rules(db, code.id, ["Names an official"], ["Only about family"], "user")
    text = code_instructions(db, code, {})
    assert "Counts when:\n- Names an official" in text
    assert "Does not count when:\n- Only about family" in text

    override = {"name": "Ignored", "definition": "Proposed.", "threshold": 0.8, "include": ["New rule"]}
    proposed = code_instructions(db, code, {}, override=override)
    assert proposed.startswith('Does this highlight fit the code "Ignored"?\nDefinition: Proposed.\n')
    assert "Counts when:\n- New rule" in proposed
    assert "Names an official" not in proposed
    assert "Does not count when:\n- Only about family" in proposed
    assert "0.8" not in proposed


def test_code_instructions_excludes_highlight(db, db_c, code, highlights):
    (h_old, old_text), (h_new, new_text) = highlights[0], highlights[1]
    judgments.judge(db, code.id, [h_old], True, db_c)
    judgments.judge(db, code.id, [h_new], True, db_c)
    texts = {h_old: old_text, h_new: new_text}
    clipped_new = " ".join(new_text.split()[:80])
    clipped_old = " ".join(old_text.split()[:80])
    assert f"- {clipped_new}" in code_instructions(db, code, texts)
    text = code_instructions(db, code, texts, exclude_highlight=h_new)
    assert f"- {clipped_new}" not in text
    assert f"- {clipped_old}" in text


def test_export_includes_rules_and_history(db, db_c, code):
    rules.set_rules(db, code.id, ["A"], [], "user")
    rules.set_rules(db, code.id, ["A", "B"], ["C"], "proposal", proposal_id=3)
    out = build_export(db, db_c)
    x = out["x_tagging"]["codes"][str(code.id)]
    assert x["rules"] == {"include": ["A", "B"], "exclude": ["C"]}
    assert [(h["include"], h["exclude"], h["source"], h["proposal_id"]) for h in x["rules_history"]] == [
        (["A"], [], "user", None),
        (["A", "B"], ["C"], "proposal", 3),
    ]
    assert all(isinstance(h["created_at"], str) for h in x["rules_history"])
    assert "Counts when:\n- A\n- B" in x["instructions"]
    assert json.loads(json.dumps(out)) == out
