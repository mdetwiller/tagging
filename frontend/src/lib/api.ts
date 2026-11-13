import type {
  AcceptProposalRequest,
  Agreement,
  Codebook,
  CodeEdit,
  CodeOut,
  CodeRevision,
  DriftResult,
  DriftSpend,
  EmbedResult,
  EmbeddingStatus,
  FamilyNote,
  HighlightReviewStatus,
  HighlightStatusResult,
  ImportResult,
  JudgmentRequest,
  Proposal,
  ReviewData,
  RunOverview,
  RunStarted,
  RunStatus,
  ScoreStarted,
} from "./types";

// Fixed: the backend's CORS allows only http://localhost:5176, and there is no Vite proxy.
export const BASE = "http://localhost:8010";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: init?.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${path}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

/** The backend's `detail` from an api error, falling back to the whole message. */
export function errorDetail(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  const brace = message.indexOf("{");
  if (brace >= 0) {
    try {
      const body = JSON.parse(message.slice(brace));
      if (typeof body.detail === "string") return body.detail;
      if (typeof body.detail?.message === "string") return body.detail.message;
      if (Array.isArray(body.detail)) {
        return body.detail
          .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? ""}`)
          .join("; ");
      }
    } catch {
      // Not JSON; show the raw message.
    }
  }
  return message;
}

/** The HTTP status at the start of an api error message, or null. */
export function errorStatus(err: unknown): number | null {
  const match = err instanceof Error ? /^(\d{3}) /.exec(err.message) : null;
  return match ? Number(match[1]) : null;
}

export const api = {
  importCodebook: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ImportResult>("/codebooks/import", { method: "POST", body: form });
  },
  currentCodebook: () => request<Codebook>("/codebooks/current"),
  editCode: (id: number, changes: CodeEdit) =>
    request<CodeOut>(`/codes/${id}`, { method: "PATCH", body: JSON.stringify(changes) }),
  codeRevisions: (id: number) => request<CodeRevision[]>(`/codes/${id}/revisions`),
  familyNote: (id: number) => request<FamilyNote>(`/codes/${id}/note`),
  /** `baseUpdatedAt` is the `updated_at` the text was based on; a newer note gives a 409. */
  saveFamilyNote: (id: number, text: string, baseUpdatedAt: string | null) =>
    request<FamilyNote>(`/codes/${id}/note`, {
      method: "PUT",
      body: JSON.stringify({ text, base_updated_at: baseUpdatedAt }),
    }),

  embeddingStatus: () => request<EmbeddingStatus>("/embeddings/status"),
  embed: () => request<EmbedResult>("/embeddings", { method: "POST", body: JSON.stringify({ confirm: true }) }),

  startRun: (codeId?: number) =>
    request<RunStarted>("/runs", {
      method: "POST",
      body: JSON.stringify(codeId === undefined ? {} : { code_id: codeId }),
    }),
  latestRun: () => request<RunStatus>("/runs/latest"),
  runsOverview: () => request<RunOverview>("/runs/overview"),
  getRun: (id: number) => request<RunStatus>(`/runs/${id}`),

  review: (codeId: number) => request<ReviewData>(`/codes/${codeId}/review`),
  agreement: (codeId: number) => request<Agreement>(`/codes/${codeId}/agreement`),
  judge: (body: JudgmentRequest) =>
    request<{ written: number }>("/judgments", { method: "POST", body: JSON.stringify(body) }),
  setHighlightStatus: (highlightIds: number[], status: HighlightReviewStatus) =>
    request<HighlightStatusResult>("/highlight_status", {
      method: "POST",
      body: JSON.stringify({ highlight_ids: highlightIds, status }),
    }),

  /** Drafts a drift proposal (a paid OpenAI call, capped per day), or returns the open one. */
  drift: (codeId: number, sessionJudgments: number) =>
    request<DriftResult>(`/codes/${codeId}/drift`, {
      method: "POST",
      body: JSON.stringify({ session_judgments: sessionJudgments }),
    }),
  proposals: (codeId: number) => request<Proposal[]>(`/codes/${codeId}/proposals`),
  driftSpend: () => request<DriftSpend>("/drift/spend"),
  /** "Test with Jev": starts scoring in the background; poll `proposal(id)`. */
  scoreProposal: (id: number) => request<ScoreStarted>(`/proposals/${id}/score`, { method: "POST" }),
  proposal: (id: number) => request<Proposal>(`/proposals/${id}`),
  acceptProposal: (id: number, body: AcceptProposalRequest) =>
    request<Proposal>(`/proposals/${id}/accept`, { method: "POST", body: JSON.stringify(body) }),
  dismissProposal: (id: number) => request<Proposal>(`/proposals/${id}/dismiss`, { method: "POST" }),
  rollbackProposal: (id: number) => request<Proposal>(`/proposals/${id}/rollback`, { method: "POST" }),

  exportUrl: () => `${BASE}/export`,
};
