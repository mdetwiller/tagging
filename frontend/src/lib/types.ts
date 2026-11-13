// Mirrors the dicts the backend routers serialize, not backend/app/schemas.py.

export type CodeStatus = "ACTIVE" | "RETIRED";

/** GET /codebooks/current `codes[]` and PATCH /codes/{id}. Children have `children: []`. */
export interface CodeOut {
  id: number;
  name: string;
  definition: string;
  parent_code_id: number | null;
  status: CodeStatus;
  threshold: number;
  display_order: number;
  exemplar_count: number;
  children: CodeOut[];
}

export interface Codebook {
  id: number;
  name: string;
  codes: CodeOut[];
  /** Human (non-AI) coding rows on this codebook's codes; a re-import leaves them behind. */
  human_judgments: number;
}

/** PATCH /codes/{id}. Omitted fields are unchanged; `parent_code_id: null` moves to the top level. */
export interface CodeEdit {
  name?: string;
  definition?: string;
  threshold?: number;
  parent_code_id?: number | null;
  status?: CodeStatus;
}

export interface CodeRevision {
  field: string;
  before: string | null;
  after: string | null;
  created_at: string;
}

export interface ImportResult {
  codebook_id: number;
  parents: number;
  children: number;
  exemplars: number;
  warnings: string[];
}

export interface EmbeddingStatus {
  highlights_total: number;
  highlights_embedded: number;
  stale_codes: number;
  est_tokens: number;
  est_usd: number;
}

export interface EmbedResult {
  tokens_used: number;
}

export type RunState = "running" | "done" | "failed";

/** "sample": random highlights judged against every active code. "find_more": one code's
 * similarity search. Runs from before the sample run existed have no kind. */
export type RunKind = "sample" | "find_more";

/** A sample run sets `few`; a find-more run sets `hard_to_find`. Both mean positives below
 * `hard_to_find_below`. A sample run's `tries` counts highlights Jev answered for the code. */
export interface RunCodeProgress {
  positives: number;
  tries: number;
  hard_to_find?: boolean;
  few?: boolean;
}

/** A failed run may carry only `kind` and `error`; a run that has not reported yet may be null. */
export interface RunSummary {
  kind?: RunKind;
  codes?: Record<string, RunCodeProgress>;
  missing_answers?: number;
  /** Requests that still failed after the client's retries; the run skipped those highlights. */
  failed_requests?: number;
  error?: string;
}

export interface RunStatus {
  id: number;
  scope_code_id: number | null;
  status: RunState;
  started_at: string | null;
  finished_at: string | null;
  requests: number;
  tokens_used: number;
  summary: RunSummary | null;
}

/** One code's result in its current sample: the newest sample run that is not running and
 * answered for the code at least once. */
export interface SampleProgress extends RunCodeProgress {
  run_id: number;
}

/** GET /runs/overview. Both maps are keyed by code id. */
export interface RunOverview {
  sample_size: number;
  samples: Record<string, SampleProgress>;
  find_more: Record<string, RunStatus>;
}

export interface RunStarted {
  run_id: number;
}

export interface Agreement {
  n: number;
  both_yes: number;
  both_no: number;
  human_only: number;
  ai_only: number;
  percent_match: number | null;
  kappa: number | null;
}

export type HumanDecision = "applied" | "not_applied";
export type HighlightReviewStatus = "not_started" | "in_progress" | "completed";

export interface OtherCodeFinal {
  code_id: number;
  name: string;
  final: boolean | null;
}

/** One highlight row of GET /codes/{id}/review. */
export interface Row {
  highlight_id: number;
  conversation_id: number | null;
  project_id: number | null;
  text: string;
  ai_probability: number | null;
  human: HumanDecision | null;
  final: boolean | null;
  status: HighlightReviewStatus;
  /** Where the row came from: the sample run, a find-more similarity pick, or a find-more
   * random pick (sample wins). Null for a final yes from none of these. */
  source: RowSource | null;
  other_codes: OtherCodeFinal[];
}

export type RowSource = "sample" | "search" | "random";

export interface ReviewData {
  code: { id: number; name: string; definition: string; threshold: number; status: CodeStatus };
  /** The code's current sample run (newest not running, with an answer for the code), or null. */
  sample_run_id: number | null;
  /** Runs with search picks for this code, newest first. */
  find_more_run_ids: number[];
  agreement: Agreement;
  /** Highlights from the latest sample or any find-more run at or above the threshold or with
   * a final yes, then other final yeses. Each highlight is in one section only. */
  found: Row[];
  /** Find-more similarity picks not found, including unscored ones. */
  below_threshold: Row[];
  /** The current sample's highlights scored for this code and find-more random picks, not found. */
  random: Row[];
}

export interface JudgmentRequest {
  code_id: number;
  highlight_ids: number[];
  applied: boolean;
}

export interface HighlightStatusResult {
  updated: number;
  status: HighlightReviewStatus;
}
