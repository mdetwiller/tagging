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

export interface RunCodeProgress {
  positives: number;
  tries: number;
  hard_to_find: boolean;
}

/** A failed run may carry only `error`; a run that has not reported yet may be null. */
export interface RunSummary {
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
  other_codes: OtherCodeFinal[];
}

export interface ReviewData {
  code: { id: number; name: string; definition: string; threshold: number; status: CodeStatus };
  run_id: number | null;
  agreement: Agreement;
  /** This run's similarity picks at or above the threshold or with a final yes, then other final yeses. */
  found: Row[];
  /** This run's remaining similarity picks, in run order (including unscored ones). */
  below_threshold: Row[];
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
