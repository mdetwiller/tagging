import { useEffect, useState } from "react";
import { api, errorDetail, errorStatus } from "../lib/api";
import type { RunStatus } from "../lib/types";

const POLL_MS = 2000;

interface Props {
  run: RunStatus | null;
  onRunChange: (run: RunStatus) => void;
  selectedCodeId: number | null;
  /** Called when a polled run stops running, so counts and agreement can refresh. */
  onRunFinished: () => void;
}

export default function RunControls({ run, onRunChange, selectedCodeId, onRunFinished }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.latestRun().then(onRunChange, (err) => {
      if (errorStatus(err) !== 404) setError(errorDetail(err));
    });
  }, [onRunChange]);

  const runId = run?.id;
  const running = run?.status === "running";
  useEffect(() => {
    if (runId === undefined || !running) return;
    let cancelled = false;
    const timer = setInterval(() => {
      api.getRun(runId).then(
        (next) => {
          if (cancelled) return;
          onRunChange(next);
          if (next.status !== "running") onRunFinished();
        },
        (err) => {
          if (!cancelled) setError(errorDetail(err));
        }
      );
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId, running, onRunChange, onRunFinished]);

  async function start(codeId?: number) {
    setStarting(true);
    setError(null);
    try {
      const { run_id } = await api.startRun(codeId);
      onRunChange(await api.getRun(run_id));
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setStarting(false);
    }
  }

  const codes = run?.summary?.codes ?? {};
  const codeCount = Object.keys(codes).length;
  const positives = Object.values(codes).reduce((sum, c) => sum + c.positives, 0);
  const hardToFind = Object.values(codes).filter((c) => c.hard_to_find).length;

  return (
    <section className="panel runs">
      <h2>Search runs</h2>
      <div className="buttons">
        <button onClick={() => start()} disabled={starting || running}>
          Run all codes
        </button>{" "}
        <button
          onClick={() => selectedCodeId !== null && start(selectedCodeId)}
          disabled={starting || running || selectedCodeId === null}
        >
          Run this code
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {run && (
        <div className="run-status">
          <p>
            Run {run.id} ({run.scope_code_id === null ? "all codes" : `code ${run.scope_code_id}`}):{" "}
            <strong className={`run-${run.status}`}>{run.status}</strong>
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
