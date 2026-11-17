import { useState } from "react";
import { errorDetail } from "../lib/api";
import type { RunStatus } from "../lib/types";

interface Props {
  run: RunStatus | null;
  /** From GET /runs/overview; null until it loads. */
  sampleSize: number | null;
  /** A polling error from the run tracker. */
  pollError: string | null;
  onStartSample: () => Promise<void>;
}

function describe(run: RunStatus): string {
  const kind = run.summary?.kind;
  if (kind === "sample") return "sample";
  if (kind === "find_more" || run.scope_code_id !== null) return `find more, code ${run.scope_code_id}`;
  return "all codes";
}

export default function RunControls({ run, sampleSize, pollError, onStartSample }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const running = run?.status === "running";

  async function start() {
    setStarting(true);
    setError(null);
    try {
      await onStartSample();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setStarting(false);
    }
  }

  const codes = run?.summary?.codes ?? {};
  const codeCount = Object.keys(codes).length;
  const positives = Object.values(codes).reduce((sum, c) => sum + c.positives, 0);
  const hardToFind = Object.values(codes).filter((c) => c.few || c.hard_to_find).length;

  return (
    <section className="panel runs">
      <h2>Search runs</h2>
      <div className="buttons">
        <button onClick={start} disabled={starting || running || sampleSize === null}>
          Run sample{sampleSize !== null && ` (${sampleSize} highlights)`}
        </button>
      </div>
      <p className="muted">Judges random highlights against every active code. Use “Find more” in a code’s view to search for one code.</p>
      {(error || pollError) && <p className="error">{error ?? pollError}</p>}
      {run && (
        <div className="run-status">
          <p>
            Run {run.id} ({describe(run)}): <strong className={`run-${run.status}`}>{run.status}</strong>
          </p>
          <p className="muted">
            {run.requests.toLocaleString()} requests, {run.tokens_used.toLocaleString()} tokens
            {codeCount > 0 && `; ${positives} positives across ${codeCount} codes, ${hardToFind} hard to find`}
            {run.summary?.missing_answers ? `; ${run.summary.missing_answers} missing answers` : ""}
            {run.summary?.failed_requests ? `; ${run.summary.failed_requests} failed requests` : ""}
          </p>
          {run.summary?.error && <p className="error">{run.summary.error}</p>}
        </div>
      )}
    </section>
  );
}
