# Tagging prototype

Applies Codestormer's reconciled codebook to Cortico highlights: Jev proposes tags, the
researcher judges them in bulk. Design: `plan/design.md`. Plan: `plan/milestone-1-plan.md`.

The Cortico snapshot `../acp_pipeline_snapshot.db` is opened read-only. Everything this app
writes goes to `db/tagging.db` (override with `TAGGING_DB_PATH`) and `data/embeddings/`
(override with `TAGGING_EMBEDDINGS_DIR`). All settings are in `backend/config.yaml`.

## Setup

```
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd frontend && npm install
```

Keys are read by variable name, never committed or printed:
- `TYPESAFE_API_KEY` (Jev): the environment, else `Tagging/.env` (see `.env.example`), else
  `../Codestormer/.env`.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL` (embeddings): the environment, else `../backend/.env`
  (Notes in the Margin's).

## Run

```
cd backend && .venv/bin/uvicorn app.main:app --port 8010
cd frontend && npm run dev
```

Then open http://localhost:5176. `GET http://localhost:8010/health` returns `{"ok":true}`.
Ports are fixed: backend 8010, frontend 5176 (`strictPort`; CORS allows only
`http://localhost:5176`, and `frontend/src/lib/api.ts` hard-codes `BASE`). Don't use
`--reload`; stop the backend by port (`lsof -ti tcp:8010 | xargs kill`).

## First use

1. Import Codestormer's `reconciled_codebook.json` in the setup panel. Codes with an empty
   definition are flagged. Re-importing creates a new codebook; the older one stays active
   and "current" is the newest by id. Judgments don't carry over: when the current codebook
   has any (`human_judgments` in `GET /codebooks/current`), choosing a file first asks you
   to confirm with "Import anyway" or "Cancel". The export covers only the current codebook
   and lists older ones with their coding counts in `x_tagging.earlier_codebooks`.
2. Embed the highlights and code queries. This is a paid OpenAI call
   (`text-embedding-3-small`, about 1.02M tokens, about $0.02) behind an estimate and a
   confirm step (`POST /embeddings` needs `{"confirm": true}`). Later runs of the job embed
   only highlights without a vector and stale code queries.
3. Run a sample ("Run sample (200 highlights)" in the sidebar): 200 random highlights
   (`search.sample_size`), each judged against every active code. It needs no embeddings.
   The code list shows each code's positives out of the sample, with ⚠ when there are fewer
   than 5.
4. For a code with few positives, open it and press "Find more for this code": a search in
   that code's similarity order (every 5th pick random) until 10 positives or 200 tries. It
   needs current embeddings; renaming a code or editing its definition makes its query stale,
   and Find more returns 409 until you re-embed (cheap, only the stale queries).
5. Review: accept, reject, or apply codes in bulk, then export one JSON file. A code's review
   combines the code's current sample (the newest sample run that is not running and has an
   answer for the code) with every Find more run for the code, listing each highlight once:
   "Found" (AI probability at or above the code's threshold, or final yes, then final yeses
   from elsewhere), the random sample (the sample's other highlights scored for this code,
   and Find more's random picks), and a collapsed "Below threshold" section (Find more's other similarity
   picks, including unscored ones). Each row is labelled sample, search or random.

## Jev cost and behaviour

- Each Jev request judges one highlight against every active code, so a request carries the
  whole codebook (about 4.4k input tokens with 57 codes). `usage.input_tokens` from each
  response is summed into the run's `tokens_used`.
- Measured: one single-code run on the real codebook (57 codes) took 168 requests and 736,054
  input tokens, and found 10 positives. At about 4,400 input tokens per request, a
  200-highlight sample run is about 0.9M input tokens (the old per-code search over all 57
  codes could reach about 11,400 requests).
- No TypeSafe price is recorded anywhere, and there is deliberately no cost warning for Jev.
- Retries: 5 attempts in total (`jev.max_attempts`), backoff from 2 s, on 429, 500, 502,
  503, 504 and network errors (`httpx.TransportError`).
- A request that still fails after its retries is counted in the run's `failed_requests`
  (shown under the run status) and the run moves on to the next highlight. The run fails at
  once on 400, 401 or 403, or after 3 batches in a row in which every request failed.

## Test

```
cd backend && .venv/bin/python -m pytest -q        # 114 tests, about 4 s
cd frontend && npx tsc -b --noEmit && npm run lint && npm run build
```

Tests need no network and no keys. They read the real snapshot (read-only) and write to a
temporary tagging database and embeddings directory that `tests/conftest.py` sets with
`setdefault`, so never export `TAGGING_DB_PATH` or `TAGGING_EMBEDDINGS_DIR` pointing at the
real `db/tagging.db` or `data/embeddings/`. Jev and embedding tests use fake clients. There
are no frontend tests.

## Recorded deviations

- `coding` stores the Cortico `highlight_id` directly; `highlight_share_id` stays null (only
  163 shares exist in the snapshot).
- Jev's Noul answer field, as found in the live probe of 2026-11-13: each question comes back
  as `{"c<code_id>": {"type": "noul", "noul": <0..1>}}`. `parse_noul` in
  `backend/app/services/jev.py` reads `noul` first.
- Mark Not Done sets `highlight_status.status = "not_started"` (Cortico's
  `bulkMarkAsNotDone` sets "Pending").
- Re-importing a codebook leaves older codebooks `ACTIVE`; the current codebook is the newest
  by id.

## Milestone 2 hook

The drift check hangs on `onSessionEnd(codeId, count)` in
`frontend/src/components/ReviewPane.tsx`, where `count` is the number of distinct highlights
judged for that code in the session. A session ends on "Done reviewing", or on leaving the
code's view after at least 5 new judgments.
