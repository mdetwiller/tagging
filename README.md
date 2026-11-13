# Tagging prototype

Applies Codestormer's reconciled codebook to Cortico highlights: Jev proposes tags, the
researcher judges them in bulk. Design: `plan/design.md`. Plan: `plan/milestone-1-plan.md`.

The Cortico snapshot `../acp_pipeline_snapshot.db` is opened read-only. Everything this app
writes goes to `db/tagging.db` (override with `TAGGING_DB_PATH`) and `data/embeddings/`
(override with `TAGGING_EMBEDDINGS_DIR`). All settings are in `backend/config.yaml`.

## Setup

```
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Keys are read by variable name, never committed:
- `TYPESAFE_API_KEY`: the environment, else `Tagging/.env` (see `.env.example`), else
  `../Codestormer/.env`.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`: the environment, else `../backend/.env`.

## Run

```
cd backend && .venv/bin/uvicorn app.main:app --port 8010
```

`GET http://localhost:8010/health` returns `{"ok":true}`. Ports are fixed: backend 8010,
frontend 5176 (CORS allows only `http://localhost:5176`). Don't use `--reload`.

## Test

```
cd backend && .venv/bin/python -m pytest -q
```

Tests need no network and no keys. They read the real snapshot (read-only) and write to a
temporary tagging database that `tests/conftest.py` sets with `setdefault`, so never export
`TAGGING_DB_PATH` pointing at the real `db/tagging.db`.

## Recorded deviations

- `coding` stores the Cortico `highlight_id` directly; `highlight_share_id` stays null (only
  163 shares exist in the snapshot).
