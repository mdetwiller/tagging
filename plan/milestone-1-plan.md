# Tagging prototype, milestone 1: implementation plan

**In plain terms:** this builds a new local app in `Tagging/` that loads Codestormer's
reconciled codebook, asks TypeSafe's Jev which Cortico highlights fit each code, and gives you
a review screen to accept or reject those suggestions one at a time or in bulk. A Python
backend reads the Cortico snapshot read-only and keeps everything you do in its own SQLite
file; a React page shows one code at a time with its found highlights, a random sample, and
how often you and Jev agree. At the end you can export the codebook and every judgment as one
Cortico-style JSON file. Drift proposals (milestone 2) are not in this plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working tag-review app for one researcher: import, embed, Jev search runs, review
with bulk actions, codebook edits with revisions, agreement, export.

**Architecture:** FastAPI + SQLAlchemy backend (`backend/`) with two engines: the Cortico
snapshot read-only and `tagging.db` read-write. Paid and external calls (OpenAI embeddings,
Jev) go through injectable clients so tests never touch the network. Vite + React 19 +
TypeScript frontend (`frontend/`) talks to the backend directly; there is no Vite proxy.

**Tech Stack:** Python 3, FastAPI 0.115.6, SQLAlchemy 2.0.36, pydantic 2.10.4, httpx 0.28.1,
openai 3.24.0, numpy, pyyaml, pytest 8.3.4 (the same pins as Notes in the Margin's
`backend/requirements.txt`, minus faster-whisper and av). React 19, Vite, TypeScript, oxlint.

**Spec:** `plan/design.md`

## Global Constraints

- Ports: backend `8010`, frontend `5176`. CORS allows only `http://localhost:5176`. `api.ts`
  uses `BASE = "http://localhost:8010"`.
- The snapshot is opened only as `sqlite:///file:<path>?mode=ro&immutable=1&uri=true` with
  `PRAGMA query_only = ON`. Never `add()` or `commit()` through the Cortico session.
- Snapshot path from config: `cortico_db_path: ../acp_pipeline_snapshot.db` (relative to
  `Tagging/`). Tagging DB from env `TAGGING_DB_PATH`, default `db/tagging.db`.
- Rows in `coding`, `code`, `code_revision`, `highlight_status` are never deleted.
- Every AI row: `is_ai_generated=True`, `status="suggested"`, `ai_metadata` with at least
  `model`, `run_id`, `question_instructions_hash`.
- Single user: `config.user_id = 101`, used for `created_by`.
- Thresholds default `0.5`. Search: `max_tries_per_code: 200`, `target_positives: 10`,
  `hard_to_find_below: 5`, `random_share: 0.2`, `jev_concurrency: 4`. All in
  `backend/config.yaml`, none hard-coded in services.
- Jev: `POST https://api.typesafe.ai/v1/systemone`, model `jev-latest`, header
  `Authorization: Bearer $TYPESAFE_API_KEY`, retry 429/5xx five times with backoff from 2 s
  (copy `call_jev` from `Codestormer/scripts/bottom_up_pipeline.py:232`). No cost warning.
- OpenAI: `text-embedding-3-small`; key and base URL read by name from
  `../backend/.env`. The embedding job runs only after a `confirm: true` request that
  follows an estimate. Never print either `.env`.
- Tests: no network, no real keys, real snapshot (read-only). `tests/conftest.py` sets
  `TAGGING_DB_PATH` to a temp file with `setdefault` before importing the app.
- Python modules start with `from __future__ import annotations`.

## Review Focus

1. **Jev returns no answer for some codes, or a non-200 after retries.** The run should record
   the gap (`missing_answers` count in the run summary), keep going with the next highlight,
   and never write an AI row with a null probability. Test in Task 5 and Task 6.
2. **A code is renamed, moved or retired while a run is in progress.** The run uses the code
   set it snapshotted at start; retired codes stop appearing in review lists but keep their
   codings. Test in Task 6.
3. **The same highlight is judged by a human several times** (accept, then remove, then
   accept). The latest human row wins for `final` and agreement; earlier rows stay. Test in
   Task 7.
4. **Starting a second run while one is active, or a run before embeddings exist.** Both
   return 409 with a message the UI shows. Test in Task 6.
5. **A code with zero overlapping judgments, or all-identical judgments.** Agreement shows
   counts, percent match `null` when n=0, and kappa `null` when expected agreement is 1
   (never a divide-by-zero 500). Test in Task 7.

---

## File structure

```
Tagging/
  .gitignore            ai-sensemaking-prototype/, .env, db/, data/, backend/.venv/, frontend/node_modules/, frontend/dist/
  .env.example          TYPESAFE_API_KEY=
  README.md             setup, run, test commands; the recorded deviations
  plan/design.md, plan/milestone-1-plan.md
  backend/
    requirements.txt
    config.yaml
    app/
      config.py         load config.yaml, resolve paths, read env vars by name
      db.py             cortico_engine (read-only), tagging_engine, sessions, Bases
      models_cortico.py read-only Highlight, Conversation (only columns used)
      models.py         Codebook, Code, CodeRevision, CodeExemplar, Coding, HighlightStatus, SearchRun, RunHighlight
      schemas.py        request bodies
      main.py           app, CORS, lifespan create_all, routers, GET /health
      services/
        codebook_import.py   parse reconciled JSON -> rows
        codebook_edit.py     rename/move/definition/threshold/retire with revisions
        embeddings.py        estimate, embed highlights and code queries, rank candidates
        jev.py               instructions builder, request builder, Noul parser, client
        search_run.py        the per-code search loop and background runner
        judgments.py         human judgments, final flag, agreement, highlight status
        export.py            export document
      routers/
        codebook.py, embeddings.py, runs.py, review.py, export.py
    tests/
      conftest.py, fixtures/reconciled_small.json, test_*.py (one per service/router)
  frontend/
    src/lib/api.ts, src/lib/types.ts
    src/App.tsx
    src/components/CodeList.tsx, ReviewPane.tsx, BulkBar.tsx, RunControls.tsx, SetupPanel.tsx
```

---

### Task 1: Repo, config, engines, models, health

**Files:**
- Create: everything under `backend/app/{config,db,models_cortico,models,main}.py`,
  `backend/config.yaml`, `backend/requirements.txt`, `backend/tests/conftest.py`,
  `backend/tests/test_foundation.py`, `.gitignore`, `.env.example`, `README.md`

**Interfaces:**
- Produces: `get_config() -> Config` (attrs `cortico_db_abspath`, `tagging_db_abspath`,
  `user_id`, `search: SearchConfig`, `jev: JevConfig`, `embeddings: EmbeddingConfig`);
  `cortico_engine`, `tagging_engine`, `CorticoSession`, `TaggingSession`, `get_cortico_db`,
  `get_tagging_db` (FastAPI dependencies); `read_env_var(name: str, env_file: Path) -> str | None`.
- Models (table names exactly): `codebook(id, name, source_file, source_last_saved,
  imported_at, status)`, `code(id, codebook_id, parent_code_id, name, definition, code_type
  default "THEMATIC", status "ACTIVE"|"RETIRED", threshold float default 0.5, display_order,
  origin_id, source, x_source JSON, query_stale bool default True)`,
  `code_revision(id, code_id, field, before JSON, after JSON, created_by, created_at)`,
  `code_exemplar(id, code_id, snippet_id, conversation_id, text, source "codestormer")`,
  `coding(id, code_id, highlight_id, conversation_id, highlight_share_id nullable,
  is_ai_generated, ai_probability nullable, status "suggested"|"applied"|"not_applied",
  created_by, created_at, run_id nullable, ai_metadata JSON nullable)`,
  `highlight_status(highlight_id PK, status "not_started"|"in_progress"|"completed",
  updated_at)`, `search_run(id, scope_code_id nullable, status
  "running"|"done"|"failed", started_at, finished_at, requests, tokens_used,
  summary JSON)`, `run_highlight(id, run_id, target_code_id, highlight_id, picked_by
  "similarity"|"random", position)`.
- Cortico read models: `Highlight(id, conversation_id, text, start_offset, end_offset,
  review_status)`, `Conversation(id, project_id, title)` (check exact column names against
  `../db/schema.sql` before writing).

- [ ] **Step 1:** `git init` in `Tagging/`, write `.gitignore` (list above) and
  `.env.example`. Add `Tagging/` to `../.git/info/exclude`, the same way `Codestormer` is
  excluded at line 8. Confirm `git -C .. status --short` no longer lists `Tagging/`.
- [ ] **Step 2:** Create the venv: `cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- [ ] **Step 3: Write failing tests** in `test_foundation.py`:
  - `test_health` — `TestClient(app).get("/health").json() == {"ok": True}`.
  - `test_snapshot_refuses_writes` — executing `INSERT INTO highlight ...` through a
    `CorticoSession` raises `sqlalchemy.exc.OperationalError`.
  - `test_snapshot_has_highlights` — `count(Highlight) == 4847`.
  - `test_tagging_tables_created` — `inspect(tagging_engine).get_table_names()` contains all
    eight table names above.
  - `test_tagging_db_is_temp` — `get_config().tagging_db_abspath` is not under `Tagging/db/`.
- [ ] **Step 4:** Run `.venv/bin/python -m pytest -q`; expect failures (no app).
- [ ] **Step 5:** Implement. `db.py` copies the pattern in
  `../backend/app/db.py:1-60` (read-only engine plus `query_only` listener; foreign keys on
  for the tagging engine). `main.py` lifespan runs `Base.metadata.create_all(tagging_engine)`.
- [ ] **Step 6:** Run tests; expect 5 passed.
- [ ] **Step 7:** Commit `Add the backend skeleton, read-only snapshot engine and tagging models`.

### Task 2: Codebook import

**Files:** Create `services/codebook_import.py`, `routers/codebook.py`,
`tests/fixtures/reconciled_small.json`, `tests/test_codebook_import.py`

**Interfaces:**
- Consumes: models from Task 1.
- Produces: `import_reconciled(db: Session, data: dict, source_file: str) -> ImportResult`
  where `ImportResult = {codebook_id: int, parents: int, children: int, exemplars: int,
  warnings: list[str]}`. Routes: `POST /codebooks/import` (multipart `file`) ->
  `ImportResult`; `GET /codebooks/current` -> `{id, name, codes: [CodeOut]}` where
  `CodeOut = {id, name, definition, parent_code_id, status, threshold, display_order,
  exemplar_count, children: [CodeOut]}` (tree, two levels, ACTIVE and RETIRED both included).
- Input shape (Codestormer `app/src/lib/types.ts:35-51`): `{version: 1, last_saved, codes:
  Code[]}`, `Code = {id, name, definition, source, rq_provenance?, highlights, exemplars:
  Entry[], children?: Code[], merged_from?, ...}`, `Entry.evidence: EvidenceTurn[]` with
  `snippet_id, conversation_id, text, span?, span_text?`.

- [ ] **Step 1: Fixture.** Hand-build `reconciled_small.json`: 2 parents, 3 children, one
  child with `definition: ""`, one exemplar entry with one evidence turn that has
  `span_text`, one with only `text`.
- [ ] **Step 2: Failing tests:**
  - `test_import_counts` — result `parents == 2`, `children == 3`, `exemplars == 2`.
  - `test_import_flags_empty_definition` — `warnings` contains the empty-definition code's
    name.
  - `test_import_parent_links` — every child row's `parent_code_id` is its parent's new id;
    `origin_id` holds the Codestormer `id`; `display_order` follows file order.
  - `test_exemplar_text_prefers_span` — the exemplar row's `text` equals `span_text` when
    present, otherwise the turn `text`.
  - `test_import_rejects_cortico_format` — a body whose codes have `parent_id` returns 400
    with "Cortico-format export" in `detail`.
  - `test_reimport_creates_new_codebook` — importing twice yields two codebooks; `GET
    /codebooks/current` returns the newest.
  - `test_import_real_file` — skipped unless
    `../../Codestormer/data/reconciled/reconciled_codebook.json` exists; asserts 5 parents
    and 52 children.
- [ ] **Step 3:** Run; expect failures.
- [ ] **Step 4:** Implement. `x_source` keeps `source`, `rq_provenance`, `merged_from` names.
  Do not import `highlights` (1,719 entries, 24 MB file); they are Codestormer evidence, not
  codings. Every new code starts `query_stale=True`.
- [ ] **Step 5:** Run; expect pass. Commit `Import Codestormer's reconciled codebook`.

### Task 3: Codebook edits with revisions

**Files:** Create `services/codebook_edit.py`, `tests/test_codebook_edit.py`; Modify
`routers/codebook.py`, `schemas.py`

**Interfaces:**
- Produces: `edit_code(db, code_id: int, changes: CodeEdit) -> Code` with
  `CodeEdit = {name?: str, definition?: str, threshold?: float, parent_code_id?: int | None,
  status?: "ACTIVE" | "RETIRED"}`; route `PATCH /codes/{code_id}` -> `CodeOut`;
  `GET /codes/{code_id}/revisions` -> `[{field, before, after, created_at}]`.

- [ ] **Step 1: Failing tests:**
  - `test_rename_writes_revision_first` — after rename, one `code_revision` with
    `field="name"`, the old and new values.
  - `test_definition_edit_marks_query_stale` — `query_stale` becomes True on name or
    definition change, stays unchanged on threshold change.
  - `test_move_child_to_other_parent` — `parent_code_id` changes; revision recorded.
  - `test_move_rejects_third_level` — moving a code under a child returns 400; moving a
    parent that has children under another parent returns 400.
  - `test_move_to_top_level` — `parent_code_id: null` makes a child a parent.
  - `test_threshold_bounds` — threshold outside `[0, 1]` returns 422.
  - `test_retire_keeps_codings` — retiring sets `status="RETIRED"`; its `coding` rows are
    still present.
  - `test_noop_edit_writes_no_revision`.
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement; one revision per changed field,
  added and flushed before the field changes. **Step 4:** Run; expect pass.
- [ ] **Step 5:** Commit `Edit codes with a revision trail`.

### Task 4: Embeddings and candidate ranking

**Files:** Create `services/embeddings.py`, `routers/embeddings.py`,
`tests/test_embeddings.py`

**Interfaces:**
- Produces:
  - `estimate(db_c, db_t) -> {highlights_total: int, highlights_embedded: int,
    stale_codes: int, est_tokens: int, est_usd: float}` (tokens = `len(text)//4`, the
    Codestormer convention; price `embeddings.usd_per_million_tokens: 0.02` from config).
  - `embed_all(db_c, db_t, client: EmbeddingClient) -> {tokens_used: int}` embeds missing
    highlights and stale code queries; vectors saved to `data/embeddings/highlights.npy` +
    `highlight_ids.json` and `data/embeddings/codes.npy` + `code_ids.json`, L2-normalised;
    clears `query_stale`.
  - `code_query_text(code: Code, exemplars: list[CodeExemplar]) -> str`:
    `"{name}. {definition}"` plus up to 3 exemplar texts, each clipped to 80 words.
  - `rank_candidates(code_id: int) -> list[int]` — highlight ids by cosine similarity,
    descending.
  - `EmbeddingClient` protocol: `embed(texts: list[str]) -> tuple[list[list[float]], int]`;
    `OpenAIEmbeddingClient` reads `OPENAI_API_KEY`/`OPENAI_BASE_URL` via `read_env_var` from
    `../backend/.env`, batches 256 texts per call.
  - Routes: `GET /embeddings/status` -> estimate; `POST /embeddings` with
    `{confirm: true}` -> `{tokens_used}`; without `confirm` -> 400.
- [ ] **Step 1: Failing tests** (fake client returning deterministic vectors, and the
  embeddings directory pointed at `tmp_path` via config override):
  - `test_estimate_counts_all_highlights` — `highlights_total == 4847`.
  - `test_post_requires_confirm` — 400 without `confirm`.
  - `test_embed_then_status_zero_remaining` — after `embed_all`,
    `highlights_embedded == highlights_total` and `stale_codes == 0`.
  - `test_embed_only_missing` — a second `embed_all` sends zero highlight texts to the
    client.
  - `test_rank_candidates_orders_by_similarity` — with a fake client that maps a known
    highlight to the code's vector, that highlight ranks first.
- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `Embed highlights and rank candidates per code`.

### Task 5: Jev client and instructions

**Files:** Create `services/jev.py`, `tests/test_jev.py`

**Interfaces:**
- Produces:
  - `build_instructions(code: Code, exemplars, accepted: list[str], rejected: list[str]) -> str`
    — definition, then "Examples that fit:" (Codestormer exemplars, then up to 3 newest
    accepted highlight texts), then "Examples that do not fit:" (up to 3 newest rejected),
    each example clipped to 80 words. Pure function; the caller picks the newest 3 from
    human `coding` rows.
  - `build_request(highlight: {id, text, conversation_title}, codes: list[tuple[int, str]],
    model: str) -> dict` — `{model, state: {about, highlight: {id, text}}, questions:
    {"c<code_id>": {"type": "noul", "instructions": <text>}}}`. `about` comes from
    `jev.state_note` in config.
  - `parse_noul(response: dict, code_ids: list[int]) -> tuple[dict[int, float], list[int]]`
    — probabilities by code id, plus ids with no answer.
  - `JevClient` protocol `judge(body: dict) -> dict`; `HttpJevClient` uses httpx with the
    retry rule in Global Constraints. Response `usage.input_tokens` is added to the run's
    `tokens_used`.
- **Unverified:** the Noul question body and the name of the probability field in the answer.
  Codestormer only uses `score`. `parse_noul` accepts `probability`, then `p`, then
  `answer` if it is a number, and Step 5 settles it.
- [ ] **Step 1: Failing tests:**
  - `test_instructions_order_and_caps` — given 5 accepted and 5 rejected, the text holds the
    definition first and exactly 3 of each, newest first.
  - `test_instructions_without_definition` — an empty definition still yields the code name
    and examples, never the string `"None"`.
  - `test_request_has_one_noul_question_per_code`.
  - `test_parse_noul_reports_missing` — a response lacking `c7` returns 7 in the missing list
    and no entry for it.
  - `test_parse_noul_rejects_out_of_range` — a probability of 1.3 is treated as missing.
- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5: One live call.** With `TYPESAFE_API_KEY` set in `Tagging/.env` (the user
  fills it), run
  `cd backend && .venv/bin/python -m app.services.jev --probe` which sends one highlight
  against 2 codes and prints the raw `answers` JSON and `usage`. Fix `parse_noul` and the
  question body to match, then add the printed response (keys only, no key material) as
  `tests/fixtures/jev_noul_response.json` with a test that parses it.
- [ ] **Step 6:** Commit `Add the Jev client and tagging instructions`.

### Task 6: Search runs

**Files:** Create `services/search_run.py`, `routers/runs.py`, `tests/test_search_run.py`

**Interfaces:**
- Consumes: `rank_candidates`, `build_instructions`, `build_request`, `parse_noul`, `JevClient`.
- Produces: `run_search(db_c, db_t, run_id: int, client: JevClient, rng: random.Random) -> None`
  (synchronous; tested directly); `start_run(scope_code_id: int | None) -> int` starts it on a
  background thread with its own sessions. Routes: `POST /runs {code_id?}` -> 202
  `{run_id}`, 409 if a run is `running`, 409 if any active code has no embedding;
  `GET /runs/{id}` -> `{status, requests, tokens_used, summary}`; `GET /runs/latest`.
  `summary = {codes: {<code_id>: {positives, tries, hard_to_find}}, missing_answers: int}`.

- [ ] **Step 1: The loop.** This is the algorithm the tests pin:

```python
codes = active codes in scope, snapshotted at start (ids, thresholds, instruction texts)
judge_codes = all active codes (every request asks about all of them)
positives = {c: 0 for c in judge_codes}; tries = {c: 0 for c in codes}
judged = set()   # highlight ids judged in this run
remaining = set(codes)
while remaining:
    code = min(remaining, key=lambda c: positives[c]); remaining.remove(code)
    queue = interleave(rank_candidates(code) minus human-judged-for-code,
                       rng-shuffled random highlights, every 5th slot random)
    for h in queue:
        if positives[code] >= cfg.target_positives or tries[code] >= cfg.max_tries_per_code:
            break
        if h in judged: continue
        tries[code] += 1; judged.add(h)
        record run_highlight(run, target=code, h, picked_by, position)
        probs, missing = parse_noul(client.judge(build_request(h, judge_codes)))
        write one AI coding per code in probs; count missing
        for c, p in probs.items(): if p >= threshold[c] and c in positives: positives[c] += 1
hard_to_find = positives[c] < cfg.hard_to_find_below
```

  Requests run `jev_concurrency` at a time within one code's queue; a positive that arrives
  after the stop condition is still recorded.
- [ ] **Step 2: Failing tests** (fake `JevClient` driven by a dict of highlight -> probs;
  fake `rank_candidates`; `random.Random(0)`):
  - `test_stops_at_ten_positives` — a code whose first 12 candidates all score 0.9 records
    exactly 10 tries (concurrency 1 in this test).
  - `test_stops_at_200_tries` — a code that never fires records 200 tries and
    `hard_to_find: true`.
  - `test_other_codes_credit` — positives for code B found while searching for A reduce B's
    tries.
  - `test_every_fifth_is_random` — positions 5, 10, 15 have `picked_by == "random"`.
  - `test_skips_human_judged` — highlights with a human row for the target code are not
    queued for it.
  - `test_missing_answer_no_null_row` — a missing code writes no AI row and increments
    `missing_answers`.
  - `test_ai_rows_are_suggested_with_metadata` — every AI row has `status="suggested"`,
    `is_ai_generated`, `ai_metadata.model`, `ai_metadata.run_id`.
  - `test_retire_during_run_uses_snapshot` — retiring a code mid-run doesn't crash; its rows
    still carry its id.
  - `test_second_run_409` and `test_run_without_embeddings_409`.
  - `test_jev_failure_marks_run_failed` — the client raising after retries leaves
    `status="failed"` with the error in `summary.error`, and already-written rows stay.
- [ ] **Step 3–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `Run per-code Jev searches`.

### Task 7: Judgments, bulk actions, final tags, agreement

**Files:** Create `services/judgments.py`, `routers/review.py`, `tests/test_judgments.py`

**Interfaces:**
- Produces:
  - `judge(db, code_id: int, highlight_ids: list[int], applied: bool) -> int` — appends one
    human row per highlight (`status="applied"|"not_applied"`, `is_ai_generated=False`).
    Accept, reject, Apply Code and Remove Code all call this.
  - `set_status(db, highlight_ids: list[int], status) -> None`; Mark Not Done is
    `status="not_started"` (check Cortico's `bulkMarkAsNotDone`, `handles.js:1830`, and
    match its meaning if it differs).
  - `final_for(db, code_id, highlight_id) -> bool | None` — latest human row if any, else
    latest AI probability >= code threshold, else None.
  - `agreement(db, code_id) -> {n: int, both_yes: int, both_no: int, human_only: int,
    ai_only: int, percent_match: float | None, kappa: float | None}` over highlights with
    both a human row and an AI row, using the latest of each.
  - Routes: `GET /codes/{id}/review?run_id=` -> `{code, agreement, found: [Row], random:
    [Row]}` where `Row = {highlight_id, conversation_id, project_id, text, ai_probability,
    human: "applied"|"not_applied"|null, final, status, other_codes: [{code_id, name,
    final}]}`; `found` = this code's run rows picked by similarity plus any highlight with a
    final yes, newest run first; `random` = run rows picked at random. Default `run_id` is
    the latest run.
    `POST /judgments {code_id, highlight_ids, applied}`; `POST /highlight_status
    {highlight_ids, status}`; `GET /codes/{id}/agreement`.
- [ ] **Step 1: Failing tests:**
  - `test_latest_human_wins` — accept, then remove, then accept: `final` True, three rows
    kept.
  - `test_ai_threshold_when_no_human` — AI 0.6 with threshold 0.5 -> True; raise threshold
    to 0.7 -> False.
  - `test_kappa_known_values` — 10 pairs with 6 agree-yes, 2 agree-no, 1 human-only,
    1 ai-only: percent_match 0.8, kappa ≈ 0.524 (compute by hand in the test).
  - `test_agreement_empty` — n=0 gives `percent_match: None, kappa: None`.
  - `test_agreement_all_identical` — all yes gives kappa None, percent_match 1.0.
  - `test_bulk_judge_many` — 3 ids -> 3 rows.
  - `test_apply_other_code_from_random_row` — judging a code Jev never scored creates a human
    row and `final` True (a Missing case).
  - `test_review_splits_found_and_random`.
  - `test_review_hides_retired_code_from_other_codes`.
  - `test_unknown_highlight_404` — judging a highlight id not in the snapshot returns 404.
- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Commit `Record human judgments and compute agreement`.

### Task 8: Export

**Files:** Create `services/export.py`, `routers/export.py`, `tests/test_export.py`

**Interfaces:**
- Produces: `build_export(db_t, db_c) -> dict`; `GET /export` returns it as a download named
  `tagging-export-<YYYY-MM-DD>.json`. Shape:
  `{exported_at, codebook: {id, name, codes: [{id, name, description, parent_id,
  code_type, status}]}, codings: [{code_id, highlight_id, highlight_share_id: null,
  conversation_id, is_ai_generated, ai_probability, status, created_by, created_at, final,
  ai_metadata}], x_tagging: {codes: {<id>: {instructions, threshold, revisions: [...],
  agreement: {...}}}, deviations: ["coding.highlight_id stored directly; highlight_share_id
  is null"]}}`. `final` is set only on the latest row per (code, highlight, human/AI) pair;
  older rows get `final: null`.
- [ ] **Step 1: Failing tests:** `test_export_codes_flat_with_parent_id`,
  `test_export_keeps_every_coding`, `test_export_final_only_on_latest`,
  `test_export_includes_revisions_and_agreement`, `test_export_is_json_serialisable`.
- [ ] **Step 2–4:** Run (fail), implement, run (pass).
- [ ] **Step 5:** Run the whole suite: `cd backend && .venv/bin/python -m pytest -q`; expect
  all pass with no network (run once with `OPENAI_API_KEY=fake TYPESAFE_API_KEY=fake`).
- [ ] **Step 6:** Commit `Export the codebook and codings`.

### Task 9: Frontend shell, setup and code list

**Files:** Create `frontend/` (Vite React-TS template, React 19, oxlint matching Notes in the
Margin's `frontend/package.json` lint script), `src/lib/api.ts`, `src/lib/types.ts`,
`src/App.tsx`, `src/components/SetupPanel.tsx`, `CodeList.tsx`, `RunControls.tsx`

**Interfaces:**
- Consumes: every route above. `types.ts` mirrors the serialized dicts (`CodeOut`,
  `ImportResult`, `EmbeddingStatus`, `RunStatus`, `ReviewData`, `Row`, `Agreement`).
- `api.ts`: one method per route; throws `Error("<status> <path>: <body>")` on non-OK.
- Behaviour:
  - **SetupPanel** shows when there's no codebook or embeddings are incomplete: file picker
    for the reconciled JSON (shows warnings after import); an "Embed highlights" button that
    first shows `est_tokens` and `est_usd` from `/embeddings/status` with Confirm/Cancel, then
    posts `{confirm: true}`.
  - **CodeList**: parents with collapsible children, each showing `positives/tries` from the
    latest run, a ⚠ when `hard_to_find`, and agreement `n` and percent. Inline rename,
    definition edit, threshold number input, retire, and move (a "Move to…" select of parents
    plus "Top level"). Edits call `PATCH /codes/{id}` and show 400 messages inline.
  - **RunControls**: "Run all codes" and "Run this code" buttons with no confirmation; while
    running, poll `GET /runs/{id}` every 2 s and show requests and per-code progress; show
    409 messages.
- [ ] **Step 1:** Scaffold, set `server.port: 5176` and `strictPort: true` in
  `vite.config.ts`.
- [ ] **Step 2:** Implement the three components.
- [ ] **Step 3:** Verify: `npx tsc -b --noEmit` exits 0; `npm run lint` exits 0;
  `npm run build` exits 0.
- [ ] **Step 4:** Commit `Add the frontend shell, setup and code list`.

### Task 10: Review pane and bulk actions

**Files:** Create `src/components/ReviewPane.tsx`, `BulkBar.tsx`; Modify `App.tsx`

**Behaviour (layout A):**
- Header: code name, threshold, agreement (`n`, percent match, kappa or "—").
- Found list, then a "Random sample" divider and the random list. Each row: checkbox,
  highlight id, conversation, AI probability, the text (first 3 lines, click to expand), its
  own ✓ and ✗ buttons, the current human mark, "+ code" (a code picker that judges another
  code as applied), and the other codes whose `final` is true as chips with × to remove.
- BulkBar appears when any row is checked: Accept, Reject, Apply Code ▾, Remove Code ▾,
  Update Status ▾ (`not_started`, `in_progress`, `completed`), Mark Not Done. Each calls the
  routes from Task 7 with all checked ids, then refetches the review data.
- Session: a counter of judgments made since this code was opened. "Done reviewing" and
  leaving the code with 5 or more new judgments both call `onSessionEnd(codeId, count)`,
  which in milestone 1 only logs and resets the counter. Milestone 2 attaches drift here.
- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Verify typecheck, lint and build as in Task 9.
- [ ] **Step 3: Browser check on scratch data.** Start the backend with
  `TAGGING_DB_PATH=<scratchpad>/tagging-e2e.db` and the frontend; import the real reconciled
  file; embed (with the user's go-ahead for the ~$0.02 OpenAI call); run one code with Jev;
  then in the browser: accept one row with its own ✓, reject one with ✗, bulk-accept 3
  checked rows, apply another code from a random row with "+ code", rename and move a child
  code, set a threshold, and download the export. Set input values from inside the page and
  stub `window.confirm` (Chrome doubles typed text). Check that agreement numbers change and
  the export contains the new rows.
- [ ] **Step 4:** Commit `Add the review pane and bulk actions`.

### Task 11: README and handover

- [ ] **Step 1:** README: setup, run commands (`cd backend && .venv/bin/uvicorn app.main:app
  --port 8010`, no `--reload`; `cd frontend && npm run dev`), test command and count,
  the keys and where they live, the recorded deviations (`highlight_id` instead of
  `highlight_share_id`; Noul response field as found in Task 5), and that milestone 2 hangs
  drift on `onSessionEnd`.
- [ ] **Step 2:** Add a `CLAUDE.md` with the Global Constraints above, the ports and the
  never-write rule for the snapshot.
- [ ] **Step 3:** Full suite, typecheck, lint and build once more; commit
  `Document setup and rules`. Do not push; there is no remote until the user creates one.

---

## Estimate

- **My active working time:** about 3 to 4 hours. That covers 11 tasks, roughly 25 backend
  files and 8 frontend files, about 10 test-and-fix cycles, and one browser pass.
- **Your time:** about 30 to 45 minutes in total:
  - 2 minutes to put `TYPESAFE_API_KEY` in `Tagging/.env` before Task 5's live call;
  - a yes for the one-time ~$0.02 embedding call in Task 10;
  - about 20 to 30 minutes of hands-on review of the running app before merge.
- **Biggest risk:** the shape of Jev's Noul question and answer is not verified. Codestormer
  only ever sent Score questions. Task 5's single live call settles it before anything
  depends on it. If Noul behaves badly for this task, the fallback is a 2-level Score
  question, which changes only `build_request` and `parse_noul`.
