from __future__ import annotations


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import OperationalError

from app.config import TAGGING_DIR, get_config, read_env_var
from app.db import CorticoSession, tagging_engine
from app.main import app
from app.models_cortico import Conversation, Highlight

TAGGING_TABLES = {
    "codebook",
    "code",
    "code_revision",
    "code_exemplar",
    "coding",
    "highlight_status",
    "search_run",
    "run_highlight",
}


def test_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}


def test_snapshot_refuses_writes():
    db = CorticoSession()
    try:
        with pytest.raises(OperationalError):
            db.execute(
                text(
                    "INSERT INTO highlight (conversation_id, start_offset, end_offset, source, "
                    "created_at) VALUES (1, 0, 1, 'human', '2026-01-01')"
                )
            )
    finally:
        db.rollback()
        db.close()


def test_snapshot_has_highlights():
    db = CorticoSession()
    try:
        assert db.scalar(select(func.count(Highlight.id))) == 4847
        first = db.scalars(select(Highlight).limit(1)).one()
        assert first.text is not None
        conversation = db.get(Conversation, first.conversation_id)
        assert conversation is not None and conversation.title
    finally:
        db.close()


def test_tagging_tables_created():
    assert TAGGING_TABLES <= set(inspect(tagging_engine).get_table_names())


def test_tagging_db_is_temp():
    real_db_dir = (TAGGING_DIR / "db").resolve()
    assert real_db_dir not in get_config().tagging_db_abspath.parents


def test_config_values():
    config = get_config()
    assert config.user_id == 101
    assert config.cortico_db_abspath == (TAGGING_DIR.parent / "acp_pipeline_snapshot.db").resolve()
    assert config.search.max_tries_per_code == 200
    assert config.search.target_positives == 10
    assert config.search.hard_to_find_below == 5
    assert config.search.random_share == 0.2
    assert config.search.jev_concurrency == 4
    assert config.jev.model == "jev-latest"
    assert config.jev.state_note
    assert config.embeddings.model == "text-embedding-3-small"
    assert config.embeddings.usd_per_million_tokens == 0.02


def test_embeddings_dir_overridable(tmp_path, monkeypatch):
    monkeypatch.setattr(get_config().embeddings, "dir", str(tmp_path))
    assert get_config().embeddings.dir_abspath == tmp_path


def test_read_env_var_prefers_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_TAGGING_VAR=from-file\n")
    monkeypatch.setenv("FAKE_TAGGING_VAR", "from-env")
    assert read_env_var("FAKE_TAGGING_VAR", env_file) == "from-env"


def test_read_env_var_falls_back_to_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_TAGGING_VAR='from-file'\nOTHER=x\n")
    monkeypatch.delenv("FAKE_TAGGING_VAR", raising=False)
    assert read_env_var("FAKE_TAGGING_VAR", env_file) == "from-file"
    assert read_env_var("MISSING_TAGGING_VAR", env_file) is None
    assert read_env_var("FAKE_TAGGING_VAR", tmp_path / "absent.env") is None


def test_typesafe_key_lookup_order(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    tagging_env = tmp_path / "tagging.env"
    codestormer_env = tmp_path / "codestormer.env"
    codestormer_env.write_text("TYPESAFE_API_KEY=fake-codestormer\n")
    config = get_config()
    monkeypatch.setattr("app.config.TYPESAFE_ENV_FILES", (tagging_env, codestormer_env))
    assert config.typesafe_api_key() == "fake-codestormer"
    tagging_env.write_text("TYPESAFE_API_KEY=fake-tagging\n")
    assert config.typesafe_api_key() == "fake-tagging"


def test_openai_settings_read_from_backend_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    env_file = tmp_path / "backend.env"
    env_file.write_text("OPENAI_API_KEY=fake-openai\nOPENAI_BASE_URL=http://fake.invalid/v1\n")
    monkeypatch.setattr("app.config.OPENAI_ENV_FILE", env_file)
    config = get_config()
    assert config.openai_api_key() == "fake-openai"
    assert config.openai_base_url() == "http://fake.invalid/v1"
