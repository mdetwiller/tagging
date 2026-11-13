from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, Codebook, Coding, DriftProposal
from app.models_cortico import Highlight
from app.services import drift, judgments, rules


class FakeDriftClient:
    """Offline stand-in for OpenAIDriftClient; records every call."""

    def __init__(self, output: dict | None = None, input_tokens: int = 1200, output_tokens: int = 300, error=None):
        self.output = output if output is not None else {
            "name": None, "definition": "People say officials ignore them.", "threshold": 0.6,
            "include": ["Names a public official or office"], "exclude": [], "rationale": "Too broad.",
        }
        self.input_tokens, self.output_tokens, self.error = input_tokens, output_tokens, error
        self.calls: list[tuple[str, dict]] = []

    def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]:
        self.calls.append((prompt, schema))
        if self.error is not None:
            raise self.error
        return dict(self.output), self.input_tokens, self.output_tokens


@pytest.fixture(scope="module")
def highlights() -> list[tuple[int, int, str]]:
    with CorticoSession() as s:
        return list(
            s.execute(select(Highlight.id, Highlight.conversation_id, Highlight.text).order_by(Highlight.id).limit(10))
        )


@pytest.fixture
def db():
    with TaggingSession() as s:
        yield s


@pytest.fixture
def codes(db) -> dict[str, int]:
    """A codebook with parent P, its children A and B (siblings), and a retired child R."""
    book = Codebook(name="drift test")
    db.add(book)
    db.flush()
    parent = Code(codebook_id=book.id, name="Civic voice", definition="Talk about being heard.")
    db.add(parent)
    db.flush()
    ids = {"P": parent.id}
    for name, definition, status in (
        ("Feeling unheard", "People in power do not listen.", "ACTIVE"),
        ("Being consulted", "People are asked for their views.", "ACTIVE"),
        ("Old code", "Retired.", "RETIRED"),
    ):
        code = Code(codebook_id=book.id, parent_code_id=parent.id, name=name, definition=definition, status=status)
        db.add(code)
        db.flush()
        ids[name] = code.id
    db.commit()
    return {"P": ids["P"], "A": ids["Feeling unheard"], "B": ids["Being consulted"], "R": ids["Old code"]}


def _ai(db, code_id: int, h, p: float) -> None:
    db.add(
        Coding(
            code_id=code_id, highlight_id=h[0], conversation_id=h[1], is_ai_generated=True,
            ai_probability=p, status="suggested", created_by=get_config().user_id,
        )
    )
    db.commit()


def _disagree(db, code_id: int, highlights) -> None:
    """Highlight 0: AI yes, human no (a disagreement). Highlight 1: both yes (an agreement)."""
    _ai(db, code_id, highlights[0], 0.9)
    judgments.judge(db, code_id, [highlights[0][0]], False)
    _ai(db, code_id, highlights[1], 0.8)
    judgments.judge(db, code_id, [highlights[1][0]], True)


def _count(db, code_id: int) -> int:
    return db.scalar(select(func.count()).select_from(DriftProposal).where(DriftProposal.code_id == code_id))


def test_no_disagreement_makes_no_call_and_no_row(db, codes, highlights):
    a = codes["A"]
    _ai(db, a, highlights[1], 0.8)
    judgments.judge(db, a, [highlights[1][0]], True)
    fake = FakeDriftClient()
    assert drift.draft(db, a, 5, fake) is None
    assert fake.calls == []
    assert _count(db, a) == 0


def test_disagreement_drafts_one_suggested_proposal(db, codes, highlights):
    a = codes["A"]
    rules.set_rules(db, a, ["Mentions government"], [], "user")
    _disagree(db, a, highlights)
    fake = FakeDriftClient()
    p = drift.draft(db, a, 7, fake)

    assert len(fake.calls) == 1
    prompt, schema = fake.calls[0]
    assert p.status == "suggested"
    assert p.base == {
        "name": "Feeling unheard", "definition": "People in power do not listen.", "threshold": 0.5,
        "include": ["Mentions government"], "exclude": [],
    }
    assert p.proposed == {
        "definition": "People say officials ignore them.", "threshold": 0.6,
        "include": ["Names a public official or office"],
    }
    assert p.rationale == "Too broad."
    assert p.disagreements == [highlights[0][0]]
    assert p.session_judgments == 7
    assert p.tokens == 1500
    meta = p.ai_metadata
    assert meta["model"] == get_config().drift.model
    assert meta["reasoning_effort"] == get_config().drift.reasoning_effort
    assert (meta["input_tokens"], meta["output_tokens"]) == (1200, 300)
    assert meta["prompt"] == prompt

    # The prompt carries the code, its rules, its neighbours and judged highlights, disagreements first.
    assert "Feeling unheard" in prompt and "People in power do not listen." in prompt
    assert "Mentions government" in prompt
    assert "Civic voice" in prompt
    assert "Being consulted" in prompt and "People are asked for their views." in prompt
    assert "Old code" not in prompt
    assert highlights[0][2] in prompt and highlights[1][2] in prompt
    assert prompt.index(highlights[0][2]) < prompt.index(highlights[1][2])
    assert "not specific highlights" in prompt

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "definition", "threshold", "include", "exclude", "rationale"}


def test_second_draft_returns_suggested_without_call(db, codes, highlights):
    a = codes["A"]
    _disagree(db, a, highlights)
    first = drift.draft(db, a, 5, FakeDriftClient())
    fake = FakeDriftClient()
    second = drift.draft(db, a, 9, fake)
    assert second.id == first.id
    assert second.session_judgments == 5
    assert fake.calls == []
    assert _count(db, a) == 1


def test_invalid_values_are_dropped(db, codes, highlights):
    a = codes["A"]
    rules.set_rules(db, a, ["Keep me"], [], "user")
    _disagree(db, a, highlights)
    fake = FakeDriftClient(
        {
            "name": "  being CONSULTED ", "definition": "  People in power do not listen. ",
            "threshold": 1.4, "include": ["  Keep me ", "", "   "], "exclude": ["", "  "],
            "rationale": "Nothing useful.",
        }
    )
    p = drift.draft(db, a, 5, fake)
    assert p.status == "no_change"
    assert p.proposed == {}
    assert p.tokens == 1500


def test_validation_keeps_valid_fields_and_caps_rules(db, codes, highlights):
    a = codes["A"]
    _disagree(db, a, highlights)
    fake = FakeDriftClient(
        {
            "name": "  Feeling ignored ", "definition": None, "threshold": 0.5,
            "include": [f"rule {i}" for i in range(10)], "exclude": [" Only about family "],
            "rationale": "r",
        }
    )
    p = drift.draft(db, a, 5, fake)
    assert p.status == "suggested"
    assert p.proposed == {
        "name": "Feeling ignored", "include": [f"rule {i}" for i in range(8)], "exclude": ["Only about family"],
    }


def test_client_failure_saves_failed_row(db, codes, highlights):
    a = codes["A"]
    _disagree(db, a, highlights)
    with pytest.raises(drift.DraftFailed) as info:
        drift.draft(db, a, 5, FakeDriftClient(error=RuntimeError("boom")))
    p = info.value.proposal
    assert p.status == "failed"
    assert p.tokens == 0
    assert "boom" in p.ai_metadata["error"]
    # A failed proposal doesn't block the next draft.
    fake = FakeDriftClient()
    assert drift.draft(db, a, 5, fake).status == "suggested"
    assert len(fake.calls) == 1


def test_not_found_and_retired(db, codes):
    with pytest.raises(drift.CodeNotFound):
        drift.draft(db, 10**9, 1, FakeDriftClient())
    with pytest.raises(drift.CodeRetired):
        drift.draft(db, codes["R"], 1, FakeDriftClient())


def _spent(db, code_id: int, tokens: int, at: datetime) -> None:
    db.add(
        DriftProposal(
            code_id=code_id, status="dismissed", base={}, proposed={}, tokens=tokens,
            created_at=at.astimezone(timezone.utc),
        )
    )
    db.commit()


def test_cap_boundary_and_yesterday_not_counted(db, codes, highlights, monkeypatch):
    a = codes["A"]
    _disagree(db, a, highlights)
    # A local noon on a day no other test writes to.
    now = datetime(2031, 3, 12, 12, 0).astimezone()
    _spent(db, codes["B"], 400_000, now - timedelta(days=1))
    _spent(db, codes["B"], 495_000, now.replace(hour=0, minute=1))
    estimate = drift.estimate_tokens(drift.build_prompt(db, db.get(Code, a)))
    assert drift.today_tokens(db, now) == 495_000

    monkeypatch.setattr(get_config().drift, "daily_token_cap", 495_000 + estimate - 1)
    fake = FakeDriftClient()
    with pytest.raises(drift.TokenCapReached):
        drift.draft(db, a, 5, fake, now=now)
    assert fake.calls == []
    assert _count(db, a) == 0

    monkeypatch.setattr(get_config().drift, "daily_token_cap", 495_000 + estimate)
    p = drift.draft(db, a, 5, fake, now=now)
    assert len(fake.calls) == 1
    assert p.status == "suggested"
    assert drift.today_tokens(db, now) == 495_000 + 1500
    assert drift.today_tokens(db, now + timedelta(days=1)) == 0


def test_review_focus_cap_example(db, codes, highlights, monkeypatch):
    """495,000 tokens today plus an estimate of 6,000 is refused (cap 500,000)."""
    a = codes["A"]
    _disagree(db, a, highlights)
    now = datetime(2031, 4, 2, 9, 30).astimezone()
    _spent(db, codes["B"], 495_000, now)
    monkeypatch.setattr(drift, "estimate_tokens", lambda prompt: 6_000)
    fake = FakeDriftClient()
    with pytest.raises(drift.TokenCapReached):
        drift.draft(db, a, 5, fake, now=now)
    assert fake.calls == []


# Routes


@pytest.fixture
def client(monkeypatch):
    fake = FakeDriftClient()
    monkeypatch.setattr(drift, "OpenAIDriftClient", lambda: fake)
    with TestClient(app) as c:
        c.fake = fake
        yield c


def test_route_no_disagreement(client, codes):
    r = client.post(f"/codes/{codes['A']}/drift", json={"session_judgments": 3})
    assert r.status_code == 200
    assert r.json() == {"status": "no_disagreement"}
    assert client.fake.calls == []


def test_route_drafts_and_lists(client, db, codes, highlights):
    a = codes["A"]
    _disagree(db, a, highlights)
    r = client.post(f"/codes/{a}/drift", json={"session_judgments": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "suggested"
    assert body["code_id"] == a
    assert body["tokens"] == 1500
    assert set(body) >= {
        "id", "base", "proposed", "applied", "rationale", "disagreements", "session_judgments",
        "ai_metadata", "score_status", "score_summary", "created_at", "decided_at",
    }
    again = client.post(f"/codes/{a}/drift", json={"session_judgments": 4}).json()
    assert again["id"] == body["id"]
    assert len(client.fake.calls) == 1

    listed = client.get(f"/codes/{a}/proposals").json()
    assert [p["id"] for p in listed] == [body["id"]]
    assert listed[0]["score_summary"] is None

    spend = client.get("/drift/spend").json()
    assert spend["cap_tokens"] == get_config().drift.daily_token_cap
    assert spend["today_tokens"] >= 1500


def test_route_errors(client, db, codes, highlights, monkeypatch):
    assert client.post("/codes/999999999/drift", json={"session_judgments": 1}).status_code == 404
    assert client.get("/codes/999999999/proposals").status_code == 404
    assert client.post(f"/codes/{codes['R']}/drift", json={"session_judgments": 1}).status_code == 409

    a = codes["A"]
    _disagree(db, a, highlights)
    monkeypatch.setattr(drift, "estimate_tokens", lambda prompt: 10**9)
    r = client.post(f"/codes/{a}/drift", json={"session_judgments": 1})
    assert r.status_code == 409
    assert r.json()["detail"] == "Daily drift token limit reached"

    monkeypatch.setattr(drift, "estimate_tokens", lambda prompt: 1)
    client.fake.error = RuntimeError("upstream down")
    r = client.post(f"/codes/{a}/drift", json={"session_judgments": 1})
    assert r.status_code == 502
    assert client.get(f"/codes/{a}/proposals").json()[0]["status"] == "failed"
