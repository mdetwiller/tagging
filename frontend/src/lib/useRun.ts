import { useCallback, useEffect, useState } from "react";
import { api, errorDetail, errorStatus } from "./api";
import type { RunStatus } from "./types";

const POLL_MS = 2000;

/** The newest run (loaded on mount), polled while it runs. Only one run can run at a time, so
 * both the sample button and "Find more" hand their new run to `start` here. */
export function useRun(onFinished: () => void) {
  const [run, setRun] = useState<RunStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.latestRun().then(setRun, (err) => {
      if (errorStatus(err) !== 404) setError(errorDetail(err));
    });
  }, []);

  const runId = run?.id;
  const running = run?.status === "running";
  useEffect(() => {
    if (runId === undefined || !running) return;
    let cancelled = false;
    const timer = setInterval(() => {
      api.getRun(runId).then(
        (next) => {
          if (cancelled) return;
          setRun(next);
          setError(null);
          if (next.status !== "running") onFinished();
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
  }, [runId, running, onFinished]);

  /** Starts a sample run (no code) or a find-more run; throws the api error (e.g. a 409). */
  const start = useCallback(async (codeId?: number) => {
    const { run_id } = await api.startRun(codeId);
    setRun(await api.getRun(run_id));
  }, []);

  return { run, error, start };
}
