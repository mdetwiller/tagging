# Tagging prototype: design decisions

Settled in a grilling session on 2026-11-13. Each line is a decision; the plan argues from it.

## What it is

Codestormer's Phase 3, built as its own app. It applies Codestormer's reconciled codebook to
Cortico highlights. Jev proposes tags, the researcher judges them in bulk, and (milestone 2)
each code's tagging prompt improves from those judgments.

## Repo and stack

- `NotesInMarginMemos/Tagging/` is its own git repo, excluded from Notes in the Margin's git
  the way Codestormer is. The `ai-sensemaking-prototype/` clone stays beside the app as
  reference, is gitignored, and is never modified.
- Rebuilt, not forked: Vite + React 19 + TypeScript frontend on port 5176; FastAPI +
  SQLAlchemy backend on port 8010. Cortico's code and prompts may be reused freely (developed
  collaboratively).

## Data

- The snapshot `acp_pipeline_snapshot.db` is opened read-only (`mode=ro&immutable=1`,
  `PRAGMA query_only = ON`), path from config. All writes go to `tagging.db`.
- The unit of tagging is the Cortico `highlight` (4,847 rows, all with `text`). Any highlight
  may be sampled regardless of its Cortico `review_status`. The snapshot's own "ACP Codebook"
  and its 217 codings are ignored.
- Tables mirror Cortico's `codebook`, `code`, `coding`. A coding stores `highlight_id`
  directly and `highlight_share_id` is null (a recorded deviation; only 163 shares exist).
- Human and AI judgments are separate `coding` rows. Rows are never deleted.

## Codebook

- Imported once from Codestormer's reconciled JSON (`{version:1, last_saved, codes:[...]}`,
  5 parents and 52 children now), not the Cortico export, because only the reconciled file
  carries exemplar text. The import flags codes with an empty definition.
- After import this app owns the codebook. Rename, move, definition edit, threshold edit and
  retire each write a revision row first. Re-importing creates a new codebook.
- Codes are never deleted; retiring is a status change.
- Each code has a **definition** (exported; only the user edits it) and **tagging
  instructions** (what Jev sees): definition, include/exclude rules (milestone 2), the newest
  3 human-accepted and 3 human-rejected highlights, and Codestormer exemplars as fixed
  positives.
- Each code's threshold starts at 0.5 and is edited by the user per code, never tuned
  automatically.

## Search runs (Jev)

- Started from a button. No cost warning for Jev.
- One Jev request judges one highlight against every active code, as Noul questions.
- **Sample run** (the sidebar's "Run sample (N highlights)", `POST /runs` with no `code_id`):
  draws `search.sample_size` (200) highlights at random, fresh each run, and judges each
  against every active code. No stop condition and no embeddings needed. A code with fewer
  than 5 positives in the sample (highlights already human-judged for it don't count) is
  flagged "few". The run's highlights are its AI coding rows; it writes no `run_highlight`
  rows. About 4.4k input tokens per request with 57 codes, so a 200-highlight sample is
  about 0.9M input tokens.
- **Find more** (a code's review view, `POST /runs` with `code_id`): walks that code's
  candidates (similarity order, with 20% random highlights mixed in) until Jev finds 10
  highlights at or above the threshold, or 200 tries. Fewer than 5 positives flags the code
  "hard to find". It needs current embeddings. Each run's random picks are fresh.
- The run's kind is stored in its summary (`"kind": "sample" | "find_more"`), and a sample
  run is one whose kind is `"sample"`. Older all-codes runs (no scope code, no kind) are not
  samples; their per-code picks count as find-more rows. Only one run runs at a time.
- A code's current sample is the newest sample run that is not running and has at least one answered highlight for that code; the review and the code list both use it, and a running sample shows only in the run status.
- Candidates are ranked by embedding similarity (`text-embedding-3-small`) between each
  highlight and the code's name, definition and exemplars. Embedding the highlights is a
  one-time paid OpenAI job behind a cost warning (about 1M tokens, about $0.02).
- AI rows are `status="suggested"` with `ai_metadata`.

## Review

- Layout A: code list on the left; the selected code's found highlights on the right, then a
  divider and the random-sample highlights.
- A code's review combines its current sample with every find-more run for it. Each
  highlight is listed once: Found (latest AI probability at or above the threshold, or final
  yes), else Random (the sample's other highlights scored for this code, and find-more random
  picks), else
  Below threshold (find-more similarity picks). Each row shows its source: sample, search or
  random (sample wins).
- Every row has its own accept and reject; checkboxes plus a bulk bar accept or reject many.
- Bulk actions: Apply Code, Remove Code, Update Status, Mark Not Done, accept/reject
  suggestions, rename/move codes. No Assign.
- "+ code" on any row applies a code Jev didn't suggest (Missing cases).
- Final tag: the latest human judgment wins where one exists, otherwise the latest AI
  probability at or above the code's threshold. Agreement per code: percent match and
  Cohen's kappa over highlights with both judgments.
- A review session ends on "Done reviewing", or on leaving the code's view after at least 5
  new judgments. (Milestone 2 hangs the drift check on this.)

## Export

One JSON file: `codebook.codes[]` (`id, name, description, parent_id, code_type`),
`codings[]` (every human and AI judgment, with `final`), and an `x_tagging` block (each
code's instructions, threshold, revision history, agreement, current include/exclude `rules`
and `rules_history`, plus `family_notes`: each parent code's current non-empty "possibly
missing codes" note).

## Drift proposals

Settled with the user on 2026-11-13:

- A proposal may change a code's **name, definition, threshold, include rules ("Counts
  when") and exclude rules ("Does not count when")**. Rules live in the append-only
  `code_rules` table (the newest row is current; no row means no rules), because adding a
  column to `code` is off-limits. Jev's instructions list them after the definition, and a
  code with no rules gets byte-identical instructions to before.
- Re-scoring with Jev runs **only on a button** ("Test with Jev"), never automatically.
- OpenAI use is limited by a **daily token cap** (`drift.daily_token_cap: 500000`, input
  plus output tokens, local calendar day), not dollars, because no OpenAI price is recorded.
  A call is refused when today's proposal tokens plus its estimate would exceed the cap.
- A proposal is drafted **only when the session's code has at least one disagreement** (a
  highlight whose latest human judgment differs from its latest AI call at the code's
  threshold).

Model, effort, cap and example limit come from the `drift:` block in `backend/config.yaml`.

## Milestones

1. Repo and backend, import, embedding, search runs, review screen and bulk actions,
   codebook edits with revisions, agreement, export.
2. Automatic drift proposals after each session (OpenAI, no warning, daily token cap in
   config), accept/edit/dismiss, re-score with Jev and show agreement before and after,
   roll back.

## Keys

`TYPESAFE_API_KEY` in `Tagging/.env`. `OPENAI_API_KEY` / `OPENAI_BASE_URL` read from
`../backend/.env` (Notes in the Margin's), checked by name only, never printed.
