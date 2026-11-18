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
  /** True for a top-level code whose current family note is not empty; always false for children. */
  has_note: boolean;
  children: CodeOut[];
}

/** GET and PUT /codes/{id}/note. Only top-level codes have notes. */
export interface FamilyNote {
  code_id: number;
  text: string;
  /** When the current note was saved; null when the family has no note yet. */
  updated_at: string | null;
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
  /** Starred as a good example of this code; Jev always sees it as one that fits. */
  starred: boolean;
  /** Marked as a facilitator turn, for every code. */
  facilitator: boolean;
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
  /** Every facilitator turn (all codes), newest mark first; left out of the sections above. */
  facilitator: Row[];
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

/** A code's editable values as a drift proposal sees them. */
export interface CodeValues {
  name: string;
  definition: string;
  threshold: number;
  include: string[];
  exclude: string[];
}

export type ProposalStatus = "suggested" | "accepted" | "dismissed" | "rolled_back" | "no_change" | "failed";
export type ScoreStatus = "none" | "running" | "done" | "failed";
/** `rules` covers both the include and the exclude list. */
export type ProposalPart = "name" | "definition" | "threshold" | "rules";

/** "Test with Jev": agreement under the current question (`before`) and the proposed one
 * (`after`). A failed score may carry only `error`. */
export interface ProposalScoreSummary {
  before?: Agreement;
  after?: Agreement;
  missing_answers?: number;
  failed_requests?: number;
  error?: string;
}

/** The values an accept wrote, plus its code_revision ids; a rollback adds its own ids. */
export interface ProposalApplied extends Partial<CodeValues> {
  edited?: boolean;
  revision_ids?: number[];
  rollback_revision_ids?: number[];
}

export interface ProposalAIMetadata {
  model: string;
  reasoning_effort: string;
  prompt: string;
  input_tokens: number;
  output_tokens: number;
  /** Set on a failed draft. */
  error?: string;
}

/** POST /codes/{id}/drift, GET /codes/{id}/proposals (newest first), GET /proposals/{id}, and
 * the accept, dismiss and rollback routes. */
export interface Proposal {
  id: number;
  code_id: number;
  status: ProposalStatus;
  /** The code's values when the proposal was drafted. */
  base: CodeValues;
  /** Only the fields that change. */
  proposed: Partial<CodeValues>;
  applied: ProposalApplied | null;
  rationale: string;
  /** Highlight ids whose latest human and AI calls differed when drafted. */
  disagreements: number[];
  session_judgments: number;
  ai_metadata: ProposalAIMetadata | null;
  tokens: number;
  score_status: ScoreStatus;
  score_summary: ProposalScoreSummary | null;
  created_at: string;
  decided_at: string | null;
  /** For a suggested proposal, the parts whose live value no longer matches `base`; accepting
   * them is refused. Empty for any other status. */
  stale_parts: ProposalPart[];
}

/** POST /codes/{id}/drift: the drafted (or already open) proposal, or no disagreement. */
export type DriftResult = Proposal | { status: "no_disagreement" };

/** GET /drift/spend: tokens of today's (local day) proposals and the daily cap. */
export interface DriftSpend {
  today_tokens: number;
  cap_tokens: number;
}

/** POST /proposals/{id}/score (202). */
export interface ScoreStarted {
  proposal_id: number;
  score_status: "running";
}

/** POST /proposals/{id}/accept. `values` may name only fields of the chosen parts. */
export interface AcceptProposalRequest {
  parts: ProposalPart[];
  values?: Partial<CodeValues>;
}

/** A saved copy of the current codebook's codes and rules (GET /codebook_versions). */
export interface CodebookVersion {
  id: number;
  name: string;
  /** "before_restore" is saved automatically just before a restore. */
  source: "user" | "before_restore";
  restored_version_id: number | null;
  code_count: number;
  created_at: string;
}

export interface VersionFieldChange {
  field: "name" | "definition" | "threshold" | "parent_code_id" | "status" | "rules";
  before: unknown;
  after: unknown;
}

/** One code that restoring a version would change (GET /codebook_versions/{id}/diff). */
export interface VersionChange {
  code_id: number;
  name: string;
  /** "retire_added": the code was added after the version was saved. */
  kind: "edit" | "retire_added";
  changes: VersionFieldChange[];
}

export interface NewCode {
  name: string;
  definition: string;
  parent_code_id: number | null;
}
