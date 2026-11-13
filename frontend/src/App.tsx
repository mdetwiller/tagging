import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorDetail, errorStatus } from "./lib/api";
import type { Codebook, CodeOut, EmbeddingStatus, RunOverview } from "./lib/types";
import { useRun } from "./lib/useRun";
import SetupPanel from "./components/SetupPanel";
import RunControls from "./components/RunControls";
import CodeList from "./components/CodeList";
import ReviewPane from "./components/ReviewPane";

function findCode(codes: CodeOut[], id: number | null): CodeOut | null {
  if (id === null) return null;
  for (const code of codes) {
    if (code.id === id) return code;
    const child = code.children.find((c) => c.id === id);
    if (child) return child;
  }
  return null;
}

/** Ends a review session. Milestone 1 only logs; milestone 2 attaches the drift check here. */
function handleReviewSessionEnd(codeId: number, judgments: number) {
  console.log(`Review session ended for code ${codeId}: ${judgments} judgments`);
}

export default function App() {
  // undefined while loading; null when no codebook has been imported.
  const [codebook, setCodebook] = useState<Codebook | null | undefined>(undefined);
  const [embedding, setEmbedding] = useState<EmbeddingStatus | null>(null);
  const [selectedCodeId, setSelectedCodeId] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runsFinished, setRunsFinished] = useState(0);
  const onRunFinished = useCallback(() => setRunsFinished((n) => n + 1), []);
  const { run, error: runError, start: startRun } = useRun(onRunFinished);
  const [overview, setOverview] = useState<RunOverview | null>(null);
  const [judgmentsChanged, setJudgmentsChanged] = useState(0);
  const onJudgmentsChanged = useCallback(() => setJudgmentsChanged((n) => n + 1), []);
  const [noteFlags, setNoteFlags] = useState<Record<number, boolean>>({});
  const onNoteSaved = useCallback(
    (codeId: number, text: string) => setNoteFlags((prev) => ({ ...prev, [codeId]: text.trim() !== "" })),
    []
  );

  const refreshCodebook = useCallback(async () => {
    try {
      setCodebook(await api.currentCodebook());
    } catch (err) {
      if (errorStatus(err) === 404) setCodebook(null);
      else setLoadError(errorDetail(err));
    }
  }, []);

  const refreshEmbedding = useCallback(async () => {
    try {
      setEmbedding(await api.embeddingStatus());
    } catch (err) {
      setLoadError(errorDetail(err));
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshCodebook(), refreshEmbedding()]);
  }, [refreshCodebook, refreshEmbedding]);

  useEffect(() => {
    api.currentCodebook().then(setCodebook, (err) => {
      if (errorStatus(err) === 404) setCodebook(null);
      else setLoadError(errorDetail(err));
    });
    api.embeddingStatus().then(setEmbedding, (err) => setLoadError(errorDetail(err)));
  }, []);

  useEffect(() => {
    api.runsOverview().then(setOverview, (err) => setLoadError(errorDetail(err)));
  }, [runsFinished]);

  const startSample = useCallback(() => startRun(), [startRun]);

  // Stale code queries (after a rename or definition edit) don't reopen setup: the codebook and
  // highlight vectors are kept, and only Find more needs the refreshed query.
  const setupIncomplete =
    codebook === null || (embedding !== null && embedding.highlights_embedded < embedding.highlights_total);
  const staleCodes = embedding?.stale_codes ?? 0;
  // The panel stays open after setup completes so the import warnings and tokens used stay readable.
  const [keepSetup, setKeepSetup] = useState(false);
  if (setupIncomplete && !keepSetup) setKeepSetup(true);
  const selected = codebook ? findCode(codebook.codes, selectedCodeId) : null;
  const activeCodes = useMemo(
    () => (codebook ? codebook.codes.flatMap((c) => [c, ...c.children]).filter((c) => c.status === "ACTIVE") : []),
    [codebook]
  );

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="app-header">
          <h1>Tagging</h1>
          <a className="export-link" href={api.exportUrl()} download>
            Export
          </a>
        </div>
        {loadError && <p className="error">Could not reach the backend: {loadError}</p>}
        {(setupIncomplete || keepSetup) && (
          <SetupPanel
            codebook={codebook ?? null}
            embedding={embedding}
            onChanged={refreshAll}
            onClose={setupIncomplete ? null : () => setKeepSetup(false)}
          />
        )}
        {!setupIncomplete && !keepSetup && codebook && (
          <section className="panel setup-summary">
            <p className="muted">
              Codebook: {codebook.name}.
              {embedding && ` ${embedding.highlights_embedded.toLocaleString()} highlights embedded.`}
            </p>
            {staleCodes > 0 && (
              <p className="warn">
                {staleCodes === 1 ? "1 edited code needs" : `${staleCodes} edited codes need`} a new query embedding
                before Find more works for it.
              </p>
            )}
            <button onClick={() => setKeepSetup(true)}>{staleCodes > 0 ? "Update embeddings" : "Open setup"}</button>
          </section>
        )}
        <RunControls
          run={run}
          sampleSize={overview?.sample_size ?? null}
          pollError={runError}
          onStartSample={startSample}
        />
        {codebook === undefined && <p className="muted">Loading codebook…</p>}
        {codebook && (
          <CodeList
            codebook={codebook}
            run={run}
            overview={overview}
            selectedCodeId={selectedCodeId}
            onSelect={setSelectedCodeId}
            onEdited={refreshAll}
            agreementKey={runsFinished + judgmentsChanged}
            noteFlags={noteFlags}
            onNoteSaved={onNoteSaved}
          />
        )}
      </aside>
      <main className="main">
        {selected && codebook ? (
          <ReviewPane
            key={selected.id}
            codeId={selected.id}
            codebook={codebook}
            activeCodes={activeCodes}
            refreshKey={runsFinished}
            run={run}
            onFindMore={startRun}
            onChanged={onJudgmentsChanged}
            onSessionEnd={handleReviewSessionEnd}
            onNoteSaved={onNoteSaved}
          />
        ) : (
          <p className="placeholder">Select a code</p>
        )}
      </main>
    </div>
  );
}
