from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_config
from app.db import CorticoSession, TaggingSession
from app.main import app
from app.models import Code, CodeExemplar
from app.models_cortico import Highlight
from app.services import embeddings
from app.services.codebook_import import import_reconciled

FIXTURE = Path(__file__).parent / "fixtures" / "reconciled_small.json"
DIM = 8


def _hash_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b - 128.0 for b in digest[:DIM]]


class FakeClient:
    """Deterministic, offline stand-in for OpenAIEmbeddingClient."""

    def __init__(self, overrides: dict[str, list[float]] | None = None):
        self.calls: list[list[str]] = []
        self.overrides = overrides or {}

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(list(texts))
        vectors = [self.overrides.get(t) or _hash_vector(t) for t in texts]
        return vectors, sum(len(t) // 4 for t in texts)


@pytest.fixture
def emb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TAGGING_EMBEDDINGS_DIR", str(tmp_path / "embeddings"))
    get_config.cache_clear()
    embeddings.clear_cache()
    yield tmp_path / "embeddings"
    get_config.cache_clear()
    embeddings.clear_cache()


@pytest.fixture
def codebook_id() -> int:
    """Imports the fixture codebook, making it the current one."""
    with TaggingSession() as s:
        return import_reconciled(s, json.loads(FIXTURE.read_text()), "reconciled_small.json")[
            "codebook_id"
        ]


@pytest.fixture
def sessions():
    db_c, db_t = CorticoSession(), TaggingSession()
    try:
        yield db_c, db_t
    finally:
        db_c.close()
        db_t.close()


def test_estimate_counts_all_highlights(emb_dir, codebook_id, sessions):
    est = embeddings.estimate(*sessions)
    assert est["highlights_total"] == 4847
    assert est["highlights_embedded"] == 0
    assert est["stale_codes"] > 0
    assert est["est_tokens"] > 0
    assert est["est_usd"] == pytest.approx(est["est_tokens"] * 0.02 / 1_000_000)


def test_post_requires_confirm(emb_dir):
    with TestClient(app) as c:
        assert c.post("/embeddings", json={}).status_code == 400
        assert c.post("/embeddings", json={"confirm": False}).status_code == 400


def test_status_route_returns_estimate(emb_dir, codebook_id):
    with TestClient(app) as c:
        body = c.get("/embeddings/status").json()
    assert body["highlights_total"] == 4847
    assert set(body) == {
        "highlights_total",
        "highlights_embedded",
        "stale_codes",
        "est_tokens",
        "est_usd",
    }


def test_embed_then_status_zero_remaining(emb_dir, codebook_id, sessions):
    db_c, db_t = sessions
    result = embeddings.embed_all(db_c, db_t, FakeClient())
    assert result["tokens_used"] > 0
    est = embeddings.estimate(db_c, db_t)
    assert est["highlights_embedded"] == est["highlights_total"] == 4847
    assert est["stale_codes"] == 0
    assert est["est_tokens"] == 0

    vecs = np.load(emb_dir / "highlights.npy")
    ids = json.loads((emb_dir / "highlight_ids.json").read_text())
    assert vecs.dtype == np.float32
    assert vecs.shape == (4847, DIM)
    assert len(ids) == 4847
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)
    assert (emb_dir / "codes.npy").exists() and (emb_dir / "code_ids.json").exists()

    db_t.expire_all()
    current = db_t.scalars(
        select(Code).where(Code.codebook_id == codebook_id, Code.status == "ACTIVE")
    ).all()
    assert current and not any(c.query_stale for c in current)


def test_embed_only_missing(emb_dir, codebook_id, sessions):
    db_c, db_t = sessions
    embeddings.embed_all(db_c, db_t, FakeClient())
    second = FakeClient()
    result = embeddings.embed_all(db_c, db_t, second)
    sent = [t for call in second.calls for t in call]
    assert sent == []
    assert result["tokens_used"] == 0


def test_rank_candidates_orders_by_similarity(emb_dir, codebook_id, sessions):
    db_c, db_t = sessions
    code = db_t.scalars(
        select(Code).where(Code.codebook_id == codebook_id, Code.status == "ACTIVE")
    ).first()
    exemplars = list(db_t.scalars(select(CodeExemplar).where(CodeExemplar.code_id == code.id)))
    query = embeddings.code_query_text(code, exemplars)
    target = db_c.scalars(select(Highlight).order_by(Highlight.id).offset(1234).limit(1)).one()
    shared = [9.0, -3.0, 4.0, 1.0, -7.0, 2.0, 5.0, -1.0]
    embeddings.embed_all(db_c, db_t, FakeClient({query: shared, target.text: shared}))

    ranked = embeddings.rank_candidates(code.id)
    assert ranked[0] == target.id
    assert len(ranked) == 4847


def test_rank_candidates_without_vector_raises(emb_dir):
    with pytest.raises(embeddings.NoCodeVector):
        embeddings.rank_candidates(10_000_000)


def test_code_query_text_clips_exemplars():
    code = Code(name="Belonging", definition="Feeling part of a group.")
    long_text = " ".join(f"w{i}" for i in range(200))
    exemplars = [CodeExemplar(text=long_text) for _ in range(5)]
    text = embeddings.code_query_text(code, exemplars)
    assert text.startswith("Belonging. Feeling part of a group.")
    assert text.count("w79") == 3
    assert "w80" not in text
