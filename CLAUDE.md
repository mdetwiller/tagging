# CLAUDE.md

Rules and pointers for working in this repo. Update CLAUDE.md in the same commit as any
change to the behaviour it describes, because a stale rule here misleads every later session.

## 1. What this project is

The Tagging prototype is Codestormer's Phase 3, built as its own single-user, local-only app.
It imports Codestormer's reconciled codebook, ranks Cortico highlights against each code by
embedding similarity (OpenAI `text-embedding-3-small`), asks Jev (TypeSafe) for a Noul
probability per code, and lets the researcher accept, reject and apply codes in bulk, with
per-code agreement and a JSON export. The stack is FastAPI + SQLAlchemy (`backend/`) and
React 19 + Vite + TypeScript (`frontend/`). Source data is `../acp_pipeline_snapshot.db`
(read-only); everything the app writes goes to `db/tagging.db` and `data/embeddings/`.
Milestone 1 is complete; milestone 2 (drift proposals) is implemented on the `milestone-2` branch.

## 2. Development commands

| Purpose | Command |
|---|---|
| Setup (backend) | `cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Setup (frontend) | `cd frontend && npm install` |
| Run backend | `cd backend && .venv/bin/uvicorn app.main:app --port 8010` (no `--reload`; `GET /health` returns `{"ok":true}`) |
| Stop backend | `lsof -ti tcp:8010 \| xargs kill` (stop by port, because `--reload` workers orphan and don't show as uvicorn) |
| Run frontend | `cd frontend && npm run dev`, then open http://localhost:5176 |
| Test | `cd backend && .venv/bin/python -m pytest -q` (206 tests, about 5 s) |
| Typecheck | `cd frontend && npx tsc -b --noEmit` |
| Lint | `cd frontend && npm run lint` (oxlint; exits 0 with no warnings) |
| Build | `cd frontend && npm run build` |

- **Ports are fixed:** backend 8010, frontend 5176 (`strictPort`), because CORS allows only `http://localhost:5176` and `frontend/src/lib/api.ts` hard-codes `BASE = "http://localhost:8010"`.
- **Never use ports 8000/5173** (Notes in the Margin) **or 5174/5175** (Codestormer), because those apps run beside this one and hold the user's data.
- **Tests never call the network** and need no real keys. Jev and OpenAI are faked. They do read the real snapshot, read-only.
- **The tests' databases:** `tests/conftest.py` sets `TAGGING_DB_PATH` and `TAGGING_EMBEDDINGS_DIR` to temp paths with `setdefault`. Never export either in your shell pointing at the real `db/tagging.db` or `data/embeddings/`, because tests would write into them.
- **Gaps:** there is no backend lint or typecheck and no frontend tests.

## 3. Rules

| Rule | Reason |
|---|---|
| Never write to `../acp_pipeline_snapshot.db`. It is opened only as `sqlite:///file:<path>?mode=ro&immutable=1&uri=true` with `PRAGMA query_only = ON`; never `add()` or `commit()` through the Cortico session. | It is the source data, shared with Notes in the Margin. |
| Snapshot path comes from `cortico_db_path` in `backend/config.yaml`; the tagging DB from `TAGGING_DB_PATH`, default `db/tagging.db`. | One place to repoint both. |
| Rows in `coding`, `code`, `code_revision`, `highlight_status`, `family_note`, `code_rules`, `drift_proposal` and `proposal_score` are never deleted. Retiring, rejecting and removing are new rows or status changes; a family note's current text, and a code's current include/exclude rules, are its newest row. | Judgments and codebook edits must stay auditable. |
| Rename, move, definition edit, threshold edit and retire each write a `code_revision` row first. | The revision history goes into the export. |
| Every AI row has `is_ai_generated=True`, `status="suggested"`, and `ai_metadata` with at least `model`, `run_id`, `question_instructions_hash`. | Only the user files judgments, and every AI row must be traceable to its run and prompt. |
| Single user: `config.user_id = 101` is used for `created_by`. | There is no auth. |
| Thresholds (default 0.5), search settings (`max_tries_per_code`, `target_positives`, `hard_to_find_below`, `random_share`, `jev_concurrency`, `sample_size`) and drift settings (`model`, `reasoning_effort`, `daily_token_cap`, `max_judged_examples`, `timeout_seconds`, `merge_rules`) live in `backend/config.yaml`; never hard-code them in services. | The user tunes them without code changes. |
| Jev: `POST https://api.typesafe.ai/v1/systemone`, model `jev-latest`, `Authorization: Bearer $TYPESAFE_API_KEY`, 5 attempts with backoff from 2 s on 429/500/502/503/504 and network errors. No cost warning. | Copied from Codestormer's `call_jev`; the user chose no warning. |
| Jev's answer is read from `{"c<code_id>": {"type": "noul", "noul": <0..1>}}`. A missing answer is counted in `missing_answers`, never saved as a null probability. | Found in the live probe of 2026-11-13. |
| Python modules start with `from __future__ import annotations`. | House style. |
| A new or changed column on an existing table: stop and ask the user how to migrate. | There are no migrations; `create_all` only adds missing tables, and `db/tagging.db` holds real judgments. |

## 4. Paid and external calls

| Call | What it costs | Rule |
|---|---|---|
| `POST /embeddings` (OpenAI) | First run about 1.02M tokens, about $0.02; later runs only stale code queries and new highlights | Requires `{"confirm": true}` after the estimate from `GET /embeddings/status` (otherwise 400). Ask the user before triggering it. |
| `POST /runs` (Jev) | One request per highlight, each asking about every active code (about 4.4k input tokens with 57 codes). With no `code_id` it is a sample run: `search.sample_size` (200) random highlights, about 0.9M input tokens. With `code_id` it is a find-more run for that code: up to 200 requests (one took 168 requests and 736,054 tokens). No TypeSafe price is recorded. | Ask the user before starting a live run. |
| `POST /codes/{id}/drift` (OpenAI) | One `drift.model` call per proposal, plus a second call that merges each multi-rule include or exclude list into one line (`drift.merge_rules`; skipped if it would pass the cap), drafted only when the code has a disagreement and no open `suggested` proposal. Refused with 409 when today's (local day) proposal tokens plus the estimate (prompt chars / 4 + 1,000) exceed `drift.daily_token_cap`. | Ask the user before triggering a live draft. Tests use a fake client; never construct `OpenAIDriftClient` in a test. |
| `POST /proposals/{id}/score` (Jev, "Test with Jev") | One request per highlight whose latest human judgment for the proposal's code is applied or not_applied, each with two questions (`cur`, `new`), or only `cur` when the proposal changes just the threshold. Writes `proposal_score` rows, never `coding`. Only a `suggested` proposal of an active code (409 otherwise). | Ask the user before starting a live score. |

- Renaming a code or editing its definition marks its query stale; a find-more run for it returns 409 until it is re-embedded. A sample run needs no embeddings.
- A run's kind is `summary["kind"]` (`"sample"` or `"find_more"`), written when the row is created; a sample run is one whose kind is `"sample"` (it has no scope code). Older all-codes runs (no scope code, no kind) are not samples; their `run_highlight` rows count as find-more rows. A code's current sample is the newest sample run that is not running and has at least one answered highlight for that code; the review and the code list both use it, and a running sample shows only in the run status. (`judgments.current_samples`). Sample runs write no `run_highlight` rows: their highlights are the AI `coding` rows with that `run_id`. Both kinds share `_Judging` in `services/search_run.py`; keep the request, failure and write rules there.
- Only one Jev job runs at a time: a search run or a proposal score starts only under `search_run._start_lock` after `search_run.check_no_jev_job` (409 otherwise). A proposal score mirrors `_Judging`'s failure rules in `drift.score_proposal`, and leaves the scored highlight out of both questions' examples (`exclude_highlight`). Startup marks orphaned running runs and scores failed.
- Keys are read by name only: `TYPESAFE_API_KEY` from the environment, `Tagging/.env`, then `../Codestormer/.env`; `OPENAI_API_KEY` and `OPENAI_BASE_URL` from the environment, then `../backend/.env`. Never print or open any `.env`; check by variable name only.

## 5. Codestormer import contract

- Import reads Codestormer's reconciled JSON (`{version, last_saved, codes:[...]}` with `children`), never the Cortico export, because only the reconciled file carries exemplar text. A Cortico-shaped file (codes with `parent_id`) is rejected with a message.
- Each code's exemplars come from that code's own `exemplars` list. Never read exemplars from `merged_from`; it holds whole copies of merged codes, and only their names are kept (in `x_source`).
- Re-importing creates a new codebook. Older codebooks stay `ACTIVE`; the current one is the newest by id. Judgments don't carry over, so the setup panel asks for an in-page confirm when the current codebook has any (`human_judgments` from `GET /codebooks/current`), and the export lists older codebooks' coding counts in `x_tagging.earlier_codebooks`.

## 6. Pointers

| Doc | Read it when |
|---|---|
| `plan/design.md` | Before changing behaviour. It is the spec: every settled decision. |
| `plan/milestone-1-plan.md` | Before structural changes. Global Constraints, Review Focus and the file map. |
| `plan/milestone-2-plan.md` | Before changing drift proposals: its tasks and Global Constraints. |
| `README.md` | Setup, first use, measured Jev cost, recorded deviations. |
| `ai-sensemaking-prototype/` | Cortico reference code. Gitignored; never modify it. |

- **Drift check:** `onSessionEnd(codeId, count)` from `frontend/src/components/ReviewPane.tsx` (`count` = distinct highlights judged for that code in the session; fires on "Done reviewing" or on leaving after `SESSION_END_MIN_JUDGMENTS = 5`) calls `api.drift` in `App.tsx`, fire-and-forget with at most one request in flight per code. Proposals live in App state per code; the review shows them in `components/ProposalPanel.tsx`.
- **Git:** there is no remote. Commit on a branch, never push.
