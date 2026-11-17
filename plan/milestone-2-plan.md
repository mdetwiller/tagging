# Milestone 2: Drift Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**In plain terms:** Sometimes you'll disagree with Jev on a code during a review session. When the session ends, the app sends that code's current prompt and your judgments to OpenAI. OpenAI drafts a proposed change, which can touch the name, the definition, the threshold, and new "counts when / doesn't count when" rules. The proposal appears in the code's review view as a side-by-side comparison. You can tick the parts you want, edit them, and accept, or dismiss the whole thing. A "Test with Jev" button re-scores the highlights you've already judged with both the current and the proposed prompt and shows agreement before and after. An accepted proposal can be rolled back.

**Goal:** Turn the researcher's review judgments into reviewable, testable, reversible improvements to each code's tagging prompt.

**Architecture:** There are three new append-only tables, `code_rules`, `drift_proposal` and `proposal_score`, plus a `drift` service (`services/drift.py`) behind an injectable OpenAI client. Accepted name, definition and threshold changes go through the existing `codebook_edit.edit_code`, so each still writes a `code_revision`. Rules are a new versioned table, because adding a column to `code` is off-limits. The Jev re-score sends one request per judged highlight, with two questions in it (current and proposed). It runs in the background under the same single-job guard as search runs.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, the OpenAI Responses API with JSON-schema output, and TypeSafe Jev. The frontend is React 19, Vite and TypeScript.

**Spec:** `plan/design.md` (the Milestones section and the "Drift proposals" section that Task 1 adds).

## Global Constraints

- Settled with the user on 2026-11-13:
  - A proposal may change the code's **name, definition, threshold, include rules and exclude rules**.
  - Re-scoring with Jev runs **on a button** ("Test with Jev"), never automatically.
  - The daily OpenAI limit is a **token cap** (`drift.daily_token_cap: 500000`, input plus output tokens), not dollars, because no OpenAI price is recorded.
  - A proposal is drafted **only when the session's code has at least one disagreement**.
- Disagreement means a highlight whose latest human judgment for the code differs from its latest AI call: AI yes is a probability at or above the code's current threshold.
- No cost warning for drift's OpenAI call, but it never runs if the tokens of today's proposals plus this call's estimate would exceed `daily_token_cap`. "Today" is the local calendar day.
- No cost warning for Jev.
- Model ids, effort, the token cap and example limits come from a new `drift:` block in `backend/config.yaml`, never hard-coded:
  - `model: gpt-5.6-luna` and `reasoning_effort: low`, the same as Notes in the Margin's `ai_review`.
  - `daily_token_cap: 500000`.
  - `max_judged_examples: 40`.
- The OpenAI key is read only via `config.openai_api_key()` and `config.openai_base_url()`. Never print or open any `.env`.
- Tests never call the network. OpenAI and Jev are faked.
- Rows in `code_rules`, `drift_proposal` and `proposal_score` are never deleted. Rollback and dismissal are status changes plus new rows.
- No new or changed column on any existing table. Only new tables (checked against `../db/schema.sql` names: `code_rules`, `drift_proposal` and `proposal_score` don't collide).
- Every OpenAI-made proposal stores `ai_metadata` with `model`, `reasoning_effort`, `prompt`, `input_tokens` and `output_tokens`.
- Every value the model returns is validated before it is saved:
  - A threshold must be within [0, 1].
  - A name must be non-empty and must not duplicate another active code name in the codebook.
  - Rules are lists of non-empty strings, at most 8 each.
  - Unchanged fields are dropped.
- Accepted name, definition and threshold changes go through `codebook_edit.edit_code`, so `code_revision` rows are written. A name or definition change marks the query stale, as today.
- Python modules start with `from __future__ import annotations`. `frontend/src/lib/types.ts` mirrors the response dicts, and every route gets one `api.*` method.

## Review Focus

1. **A second session end for a code while a proposal is still `suggested`.** It must return that proposal and make no new OpenAI call (no duplicate spend). Test it in Task 3.
2. **Rolling back after the user hand-edited the code since accepting.** It must return 409 and name the field that changed, never silently overwrite their edit. Test it in Task 5.
3. **Leakage in "Test with Jev".** The highlight being scored must not appear among the prompt's examples, or both scores are inflated. Test it in Task 4 by asserting the scored text is absent from both questions' instructions.
4. **Proposals over a rename or move.** A proposal drafted before the user renamed the code is stale. Accepting it returns 409 when any field it changes no longer matches its stored `base`. Test it in Task 5.
5. **The cap at the boundary.** Today's 495,000 tokens plus an estimate of 6,000 is refused with 409 and no call. Proposals from yesterday don't count. Test it in Task 3 with an injected clock.

---

### Task 1: Rules table, config, and rules in Jev's prompt

**Files:**
- Modify: `backend/app/models.py` (add `CodeRules`, `DriftProposal`, `ProposalScore`), `backend/app/config.py` (`DriftConfig`), `backend/config.yaml`, `backend/app/services/jev.py`, `backend/app/services/search_run.py`, `backend/app/services/export.py`, `plan/design.md`
- Create: `backend/app/services/rules.py`
- Test: `backend/tests/test_rules.py`

**Interfaces:**
- Produces the tables:
  - `code_rules(id, code_id FK, include JSON list[str], exclude JSON list[str], source str "proposal"|"rollback"|"user", proposal_id int|null, created_at)`. The current rules are the newest row; with no row, both lists are empty.
  - `drift_proposal(id, code_id FK, status str "suggested"|"accepted"|"dismissed"|"rolled_back"|"no_change"|"failed", base JSON, proposed JSON, applied JSON|null, rationale text, disagreements JSON list[int], session_judgments int, ai_metadata JSON, tokens int, score_status str "none"|"running"|"done"|"failed", score_summary JSON|null, created_at, decided_at)`.
    - `base` and `proposed` share the shape `{name, definition, threshold, include, exclude}`. `proposed` holds only the fields that change.
  - `proposal_score(id, proposal_id FK, highlight_id, human str "applied"|"not_applied", current_p float|null, proposed_p float|null, created_at)`.
- Produces `rules.current_rules(db, code_id) -> tuple[list[str], list[str]]`.
- Produces `rules.set_rules(db, code_id, include, exclude, source, proposal_id=None) -> CodeRules`.
- Produces `jev.build_instructions(code, exemplars, accepted, rejected, include=(), exclude=(), name=None, definition=None)`. `name` and `definition` override the code's own values, for scoring a proposal.
  - After the definition it prints "Counts when:" and "Does not count when:" bullet lists, each only when non-empty.
- Changes `search_run.code_instructions(db_t, code, texts, exclude_highlight=None, override=None)`. It passes the current rules, and an override dict shaped like `proposed` takes precedence. `exclude_highlight` drops that highlight from the human examples.

- [ ] Write `test_rules.py`:
  - `current_rules` returns `([], [])` with no row, and the newest row's lists after two `set_rules` calls.
  - `build_instructions` with rules contains `"Counts when:\n- <rule>"` and `"Does not count when:\n- <rule>"`. Without rules, the output is byte-identical to today's (so existing instruction hashes don't change).
  - `code_instructions(..., exclude_highlight=h)` leaves out h's text even when h is the newest accepted highlight.
- [ ] Run `cd backend && .venv/bin/python -m pytest tests/test_rules.py -q`. It is expected to FAIL.
- [ ] Implement the models, `DriftConfig` and the `drift:` block (values in Global Constraints), then `rules.py`, the `build_instructions` and `code_instructions` changes, and an export addition: `x_tagging.codes[].rules` (current) and `x_tagging.codes[].rules_history`.
- [ ] Add a "Drift proposals" section to `plan/design.md` recording the four decisions in Global Constraints.
- [ ] Run the full suite: `.venv/bin/python -m pytest -q`. Expected: all pass (126 plus the new ones).
- [ ] Commit: `git commit -m "Add versioned code rules and show them to Jev"`

### Task 2: Shared agreement helper

**Files:**
- Modify: `backend/app/services/judgments.py`
- Test: `backend/tests/test_judgments.py`

**Interfaces:**
- Produces `judgments.agreement_stats(pairs: Iterable[tuple[bool, bool]]) -> dict` over (human, ai) pairs. It returns the same keys as `agreement` today: `n`, `both_yes`, `both_no`, `human_only`, `ai_only`, `percent_match` and `kappa`. `agreement()` calls it.
- Produces `judgments.disagreements(db, code_id) -> list[int]`: the highlight ids whose latest human and latest AI call for the code differ.

- [ ] Tests:
  - `agreement_stats([])` gives `n == 0` and `kappa is None`.
  - `agreement_stats([(True, True), (False, False), (True, False), (False, True)])` gives `percent_match == 0.5` and `kappa == 0.0`.
  - `disagreements` lists exactly the seeded mismatches and ignores highlights with only one side.
- [ ] Implement it. The existing agreement tests stay green.
- [ ] Commit: `git commit -m "Factor agreement stats out for proposal scoring"`

### Task 3: Drafting a proposal (OpenAI)

**Files:**
- Create: `backend/app/services/drift.py`, `backend/app/routers/drift.py`
- Modify: `backend/app/main.py` (include router), `backend/app/schemas.py`
- Test: `backend/tests/test_drift.py`

**Interfaces:**
- `class DriftClient(Protocol): def propose(self, prompt: str, schema: dict) -> tuple[dict, int, int]` returns (parsed JSON, input tokens, output tokens). `OpenAIDriftClient` uses `responses.create(model, reasoning={"effort"}, input=prompt, text={"format": {"type": "json_schema", "strict": True, ...}})`, the same pattern as `../backend/app/services/ai_review.py:235`.
- `drift.draft(db, code_id, session_judgments, client, now=None) -> DriftProposal`.
- Route `POST /codes/{id}/drift` with body `{session_judgments: int}` returns `{status: "no_disagreement"}` or a serialized proposal.
- `GET /codes/{id}/proposals` returns the newest first, each with its scores summary.
- `GET /drift/spend` returns `{today_tokens, cap_tokens}`.

The model's output schema is `{name: str|null, definition: str|null, threshold: number|null, include: [str], exclude: [str], rationale: str}`. Null or an unchanged value means no change.

The prompt carries:
- the code's name, definition, threshold and current rules;
- its parent's name and its siblings' names and definitions, so changes don't overlap neighbours;
- up to `max_judged_examples` judged highlights, disagreements first, each with its text, the human verdict and the AI probability;
- the instruction that rules describe what the code includes or excludes, not specific highlights.

Order of checks in `draft`:
1. The code is not found or retired: 404 or 409.
2. An existing `suggested` proposal for the code: return it, with no call.
3. `disagreements()` is empty: return `{status: "no_disagreement"}`, with no call and no row.
4. Estimate the tokens as (prompt chars / 4) input + 1,000 output. If today's `tokens` plus the estimate exceeds the cap: 409 "Daily drift token limit reached".
5. Call, validate (Global Constraints), and save. If nothing survives validation, the status is `no_change`. A client exception saves status `failed` with the error in `ai_metadata` and returns 502.

- [ ] Tests with a fake client that records calls:
  - no disagreement means no call;
  - a disagreement produces one call and a `suggested` row with `base` filled and `tokens` = the fake input plus output tokens;
  - a second call returns the same row (Review Focus 1);
  - a threshold of 1.4, a duplicate name and empty rules are dropped, and all-dropped becomes `no_change`;
  - the cap boundary with an injected `now`, and yesterday not counted (Review Focus 5).
- [ ] Implement it, run the suite, and commit: `git commit -m "Draft drift proposals from review disagreements"`

### Task 4: "Test with Jev" re-score

**Files:**
- Modify: `backend/app/services/drift.py`, `backend/app/routers/drift.py`, `backend/app/services/search_run.py` (the shared job guard)
- Test: `backend/tests/test_drift_score.py`

**Interfaces:**
- `drift.start_score(proposal_id) -> None` sets `score_status="running"` and runs in a background thread like `search_run.start_run`. It returns 409 if any search run or other score is running, and a search run's start returns 409 while a score runs. A server restart marks orphaned `running` scores `failed`, alongside `fail_orphaned_runs`.
- `POST /proposals/{id}/score` returns 202. `GET /proposals/{id}` returns the proposal with `score_status` and `score_summary = {before: agreement_stats, after: agreement_stats, missing_answers, failed_requests}`.
- For each highlight with a latest human judgment for the code (the judged set at scoring time), one Jev request asks two questions:
  - `cur`: `code_instructions(exclude_highlight=h)`;
  - `new`: `code_instructions(exclude_highlight=h, override=proposed)`.
  - "Before" uses the code's current threshold; "after" uses the proposed threshold, if any.
  - Concurrency, retries and failure rules match `_Judging`: count failed requests and continue, and fail on 400/401/403.
  - Rows are written to `proposal_score`, never to `coding`.

- [ ] Tests with a fake Jev:
  - one request per judged highlight, each with keys `cur` and `new`;
  - the scored highlight's text is absent from both instructions (Review Focus 3);
  - the before and after summaries match hand-computed `agreement_stats`, using the proposed threshold for after;
  - no `coding` rows are added;
  - 409 while a search run is running, and the reverse.
- [ ] Implement it, run the suite, and commit: `git commit -m "Re-score a proposal against judged highlights with Jev"`

### Task 5: Accept, edit, dismiss, roll back

**Files:**
- Modify: `backend/app/services/drift.py`, `backend/app/routers/drift.py`, `backend/app/schemas.py`, `backend/app/services/export.py`
- Test: `backend/tests/test_drift_decide.py`

**Interfaces:**
- `POST /proposals/{id}/accept` with body `{parts: list["name"|"definition"|"threshold"|"rules"], values?: {name?, definition?, threshold?, include?, exclude?}}`. `values` are the user's edits and win over `proposed`.
  - Returns 409 unless the status is `suggested`.
  - Returns 409 naming the field when any accepted field's current value no longer equals `base` (Review Focus 4).
  - It applies name, definition and threshold in one `edit_code` call, so the revision reason carries `proposal #id`, and rules through `set_rules(source="proposal")`.
  - It sets `applied` to the final applied values (with `edited: true` when `values` differ from `proposed`), the status to `accepted`, and `decided_at`.
- `POST /proposals/{id}/dismiss` only works on `suggested`.
- `POST /proposals/{id}/rollback` only works on the code's newest `accepted` proposal.
  - Returns 409 naming the field if the code's current value for any applied field differs from `applied` (Review Focus 2).
  - It restores `base` values via `edit_code` and `set_rules(source="rollback")` and sets the status to `rolled_back`.
- Export adds `x_tagging.codes[].proposals`: id, status, base, proposed, applied, rationale, score_summary and tokens.

- [ ] Tests:
  - accept with parts `["threshold","rules"]` changes only those and writes one `code_revision` and one `code_rules`;
  - an edited value is applied and flagged;
  - accept after a manual rename of a proposal that renames gives 409;
  - rollback restores everything and writes new rows (row counts only go up);
  - rollback after a manual threshold edit gives 409;
  - dismiss then accept gives 409;
  - the export includes proposals.
- [ ] Implement it, run the suite, and commit: `git commit -m "Accept, edit, dismiss and roll back drift proposals"`

### Task 6: Frontend

**Files:**
- Create: `frontend/src/components/ProposalPanel.tsx`
- Modify: `frontend/src/App.tsx` (wire `onSessionEnd` to `api.drift`), `frontend/src/components/ReviewPane.tsx`, `frontend/src/components/CodeList.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`, `frontend/src/index.css`, `CLAUDE.md`, `README.md`

The panel's behaviour:
- `onSessionEnd(codeId, count)` calls `api.drift(codeId, count)`, fire-and-forget. The result shows in that code's review view, and its error (token limit reached) shows as one line there.
- The panel sits at the top of the review, under the family notes, when the code has a `suggested` proposal.
  - It shows the rationale and, for each changed part (name, definition, threshold, rules), the current value beside the proposed one, with a checkbox (ticked by default) and an editable field.
  - The "Based on N disagreements" link scrolls to them.
  - There are "Test with Jev" (which polls `GET /proposals/{id}` every 2 s), "Accept selected" and "Dismiss" buttons.
  - After a test it shows a before/after table of n, percent match and kappa.
- "Proposal history" is collapsed. It lists past proposals with their status and has a "Roll back" button on the newest accepted one. Accepting or rolling back refreshes the codebook (the name and threshold may change).
- CodeList shows a "proposal" marker on codes with a `suggested` proposal.
- The sidebar shows "Drift tokens today: x of 500,000" (the cap from `GET /drift/spend`).

- [ ] Implement it. `npx tsc -b --noEmit`, `npm run lint` and `npm run build` must be clean.
- [ ] Update CLAUDE.md (§1 milestone status, §3 rules for the three tables, §4 a drift row: OpenAI, no warning, daily token cap from config) and the README.
- [ ] Commit: `git commit -m "Show, test and decide drift proposals in the review view"`

## Verification at the end

- The full backend suite passes, and the frontend typecheck, lint and build are clean.
- In the browser, with the scratch copy of `db/tagging.db` via `TAGGING_DB_PATH`, the panel renders from a seeded proposal. No live OpenAI or Jev call is made without the user's go-ahead.
