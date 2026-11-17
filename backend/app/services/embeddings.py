from __future__ import annotations

import json
import os
from typing import Protocol

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Code, CodeExemplar, Codebook
from ..models_cortico import Highlight

MAX_EXEMPLARS = 3
EXEMPLAR_WORDS = 80
OPENAI_BATCH = 256

# Each kind is a vector file plus an id file whose order matches the vector rows.
HIGHLIGHTS = ("highlights.npy", "highlight_ids.json")
CODES = ("codes.npy", "code_ids.json")


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Vectors in input order, and the tokens the call used."""
        ...


class NoCodeVector(LookupError):
    pass


class OpenAIEmbeddingClient:
    """Paid. Never construct this in tests."""

    def __init__(self) -> None:
        config = get_config()
        api_key = config.openai_api_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment or backend/.env.")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=config.openai_base_url() or None)
        self._model = config.embeddings.model

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), OPENAI_BATCH):
            response = self._client.embeddings.create(
                model=self._model, input=texts[start : start + OPENAI_BATCH]
            )
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
            tokens += response.usage.total_tokens
        return vectors, tokens


_cache: dict[tuple[str, str], tuple[list[int], np.ndarray]] = {}


def clear_cache() -> None:
    _cache.clear()


def _load(kind: tuple[str, str]) -> tuple[list[int], np.ndarray]:
    if kind in _cache:
        return _cache[kind]
    directory = get_config().embeddings.dir_abspath
    vec_path, ids_path = directory / kind[0], directory / kind[1]
    if not vec_path.exists() or not ids_path.exists():
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = json.loads(ids_path.read_text())
    vectors = np.load(vec_path)
    if len(ids) != vectors.shape[0]:
        # A crash between the two renames in _save; the next embed_all redoes the pair.
        return [], np.zeros((0, 0), dtype=np.float32)
    _cache[kind] = (ids, vectors)
    return ids, vectors


def _save(kind: tuple[str, str], ids: list[int], vectors: np.ndarray) -> None:
    directory = get_config().embeddings.dir_abspath
    vec_path, ids_path = directory / kind[0], directory / kind[1]
    vec_tmp, ids_tmp = vec_path.with_suffix(".npy.tmp"), ids_path.with_suffix(".json.tmp")
    with open(vec_tmp, "wb") as f:
        np.save(f, vectors.astype(np.float32))
    ids_tmp.write_text(json.dumps(ids))
    os.replace(vec_tmp, vec_path)
    os.replace(ids_tmp, ids_path)
    _cache[kind] = (list(ids), vectors.astype(np.float32))


def _normalise(vectors: list[list[float]]) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.where(norms == 0, 1, norms)


def _merge(
    kind: tuple[str, str], new_ids: list[int], new_vectors: np.ndarray
) -> None:
    """Replaces rows for ids already stored and appends the rest, then saves."""
    ids, vectors = _load(kind)
    ids = list(ids)
    if vectors.size and vectors.shape[1] != new_vectors.shape[1]:
        raise ValueError(
            f"Stored {kind[0]} vectors have dimension {vectors.shape[1]}, new ones "
            f"{new_vectors.shape[1]}. Delete the embeddings directory to re-embed everything."
        )
    vectors = vectors.copy() if vectors.size else np.zeros((0, new_vectors.shape[1]), np.float32)
    row_of = {hid: i for i, hid in enumerate(ids)}
    appended: list[np.ndarray] = []
    for hid, vec in zip(new_ids, new_vectors):
        if hid in row_of:
            vectors[row_of[hid]] = vec
        else:
            row_of[hid] = len(ids)
            ids.append(hid)
            appended.append(vec)
    if appended:
        vectors = np.vstack([vectors, np.stack(appended)])
    _save(kind, ids, vectors)


def code_query_text(code: Code, exemplars: list[CodeExemplar]) -> str:
    parts = [f"{code.name}. {code.definition}"]
    for exemplar in exemplars[:MAX_EXEMPLARS]:
        parts.append(" ".join(exemplar.text.split()[:EXEMPLAR_WORDS]))
    return "\n".join(parts)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _current_active_codes(db_t: Session) -> list[Code]:
    codebook = db_t.scalars(select(Codebook).order_by(Codebook.id.desc()).limit(1)).first()
    if codebook is None:
        return []
    return list(
        db_t.scalars(
            select(Code)
            .where(Code.codebook_id == codebook.id, Code.status == "ACTIVE")
            .order_by(Code.id)
        )
    )


def _stale_codes(db_t: Session) -> list[Code]:
    embedded = set(_load(CODES)[0])
    return [c for c in _current_active_codes(db_t) if c.query_stale or c.id not in embedded]


def stale_code_ids(db_t: Session) -> set[int]:
    """Active codes of the current codebook with no query vector, or one older than their
    name or definition."""
    return {c.id for c in _stale_codes(db_t)}


def current_active_codes(db_t: Session) -> list[Code]:
    return _current_active_codes(db_t)


def _code_texts(db_t: Session, codes: list[Code]) -> list[str]:
    texts = []
    for code in codes:
        exemplars = list(
            db_t.scalars(
                select(CodeExemplar)
                .where(CodeExemplar.code_id == code.id)
                .order_by(CodeExemplar.id)
                .limit(MAX_EXEMPLARS)
            )
        )
        texts.append(code_query_text(code, exemplars))
    return texts


def _missing_highlights(db_c: Session) -> tuple[int, list[tuple[int, str]]]:
    embedded = set(_load(HIGHLIGHTS)[0])
    rows = db_c.execute(select(Highlight.id, Highlight.text).order_by(Highlight.id)).all()
    return len(rows), [(hid, text) for hid, text in rows if hid not in embedded]


def estimate(db_c: Session, db_t: Session) -> dict:
    total, missing = _missing_highlights(db_c)
    stale = _stale_codes(db_t)
    tokens = sum(_estimate_tokens(t) for _, t in missing)
    tokens += sum(_estimate_tokens(t) for t in _code_texts(db_t, stale))
    return {
        "highlights_total": total,
        "highlights_embedded": total - len(missing),
        "stale_codes": len(stale),
        "est_tokens": tokens,
        "est_usd": tokens * get_config().embeddings.usd_per_million_tokens / 1_000_000,
    }


def embed_all(db_c: Session, db_t: Session, client: EmbeddingClient) -> dict:
    """Embeds highlights without a vector and stale code queries. Saves after every batch,
    so an interrupted run keeps what it paid for and the next run resumes."""
    batch_size = get_config().embeddings.batch_size
    tokens_used = 0

    _, missing = _missing_highlights(db_c)
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        vectors, tokens = client.embed([text for _, text in batch])
        tokens_used += tokens
        _merge(HIGHLIGHTS, [hid for hid, _ in batch], _normalise(vectors))

    stale = _stale_codes(db_t)
    texts = _code_texts(db_t, stale)
    for start in range(0, len(stale), batch_size):
        batch = stale[start : start + batch_size]
        vectors, tokens = client.embed(texts[start : start + batch_size])
        tokens_used += tokens
        _merge(CODES, [c.id for c in batch], _normalise(vectors))
        for code in batch:
            code.query_stale = False
        db_t.commit()

    return {"tokens_used": tokens_used}


def rank_candidates(code_id: int) -> list[int]:
    """Highlight ids by cosine similarity to the code's query vector, most similar first."""
    code_ids, code_vectors = _load(CODES)
    try:
        query = code_vectors[code_ids.index(code_id)]
    except ValueError:
        raise NoCodeVector(
            f"Code {code_id} has no query embedding yet; run POST /embeddings first."
        ) from None
    highlight_ids, highlight_vectors = _load(HIGHLIGHTS)
    if not highlight_ids:
        return []
    order = np.argsort(-(highlight_vectors @ query), kind="stable")
    return [highlight_ids[i] for i in order]
