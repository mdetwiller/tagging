import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorDetail, errorStatus } from "./lib/api";
import type { Codebook, CodeOut, DriftSpend, EmbeddingStatus, Proposal, RunOverview } from "./lib/types";
import { useRun } from "./lib/useRun";
import SetupPanel from "./components/SetupPanel";
import RunControls from "./components/RunControls";
import CodeList from "./components/CodeList";
import ReviewPane from "./components/ReviewPane";
import VersionsPanel from "./components/VersionsPanel";
import type { DriftState } from "./components/ProposalPanel";

function findCode(codes: CodeOut[], id: number | null): CodeOut | null {
  if (id === null) return null;
  for (const code of codes) {
    if (code.id === id) return code;
    const child = code.children.find((c) => c.id === id);
    if (child) return child;
  }
  return null;
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

  // Drift proposals by code id, newest first. Only codes whose proposals have been fetched have
  // an entry; each fetch is numbered so an older response can't overwrite a newer one.
  const [proposals, setProposals] = useState<Record<number, Proposal[]>>({});
  const proposalRequests = useRef<Record<number, number>>({});
  const [driftStates, setDriftStates] = useState<Record<number, DriftState>>({});
  const drafting = useRef(new Set<number>());
  const [spend, setSpend] = useState<DriftSpend | null>(null);

  const refreshProposals = useCallback((codeId: number): Promise<void> => {
    const request = (proposalRequests.current[codeId] ?? 0) + 1;
    proposalRequests.current[codeId] = request;
    return api.proposals(codeId).then(
      (list) => {
        if (proposalRequests.current[codeId] === request) setProposals((prev) => ({ ...prev, [codeId]: list }));
      },
      // One code's proposals failing to load is not a backend outage; the code just shows none.
      (err) => console.warn(`Could not load proposals for code ${codeId}: ${errorDetail(err)}`)
    );
  }, []);

  const onProposalUpdated = useCallback((proposal: Proposal) => {
    setProposals((prev) => {
      const list = prev[proposal.code_id];
      if (!list) return prev;
      return { ...prev, [proposal.code_id]: list.map((p) => (p.id === proposal.id ? proposal : p)) };
    });
  }, []);

  const refreshSpend = useCallback(() => {
    api.driftSpend().then(setSpend, (err) => setLoadError(errorDetail(err)));
  }, []);

  // The drift check: fire-and-forget, at most one in flight per code, so an effect that fires
  // twice can't draft twice. Its outcome shows in that code's review view.
  const onSessionEnd = useCallback(
    (codeId: number, judgments: number) => {
      if (drafting.current.has(codeId)) return;
      drafting.current.add(codeId);
      setDriftStates((prev) => ({ ...prev, [codeId]: { kind: "drafting" } }));
      const settle = (state: DriftState | null) =>
        setDriftStates((prev) => {
          const next = { ...prev };
          if (state) next[codeId] = state;
          else delete next[codeId];
          return next;
        });
      api
        .drift(codeId, judgments)
        .then(
          (result) => settle("id" in result && result.status === "no_change" ? { kind: "no_change" } : null),
          (err) => settle({ kind: "error", message: errorDetail(err) })
        )
        .finally(() => {
          drafting.current.delete(codeId);
          refreshProposals(codeId);
          refreshSpend();
        });
    },
    [refreshProposals, refreshSpend]
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

  useEffect(() => {
    refreshSpend();
  }, [refreshSpend]);

  // Every code's proposals once per set of codes (like the code list's agreement), so the
  // "proposal" markers show without opening each code; later fetches are per code.
  const codeIdsKey = useMemo(
    () => (codebook ? codebook.codes.flatMap((c) => [c, ...c.children]).map((c) => c.id).join(",") : ""),
    [codebook]
  );
  useEffect(() => {
    if (codeIdsKey === "") return;
    for (const id of codeIdsKey.split(",").map(Number)) refreshProposals(id);
  }, [codeIdsKey, refreshProposals]);

  const onProposalDecided = useCallback(
    async (codeId: number) => {
      await Promise.all([refreshAll(), refreshProposals(codeId)]);
    },
    [refreshAll, refreshProposals]
  );

  // A restore can change any code, so every loaded code's proposals are refetched for their stale marks.
  const onRestored = useCallback(async () => {
    await refreshAll();
    await Promise.all(Object.keys(proposals).map((id) => refreshProposals(Number(id))));
  }, [refreshAll, refreshProposals, proposals]);

  const proposalCodeIds = useMemo(
    () =>
      new Set(
        Object.entries(proposals)
          .filter(([, list]) => list.some((p) => p.status === "suggested"))
          .map(([id]) => Number(id))
      ),
    [proposals]
  );

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
        {spend && (
          <p className="muted drift-spend">
            Drift tokens today: {spend.today_tokens.toLocaleString()} of {spend.cap_tokens.toLocaleString()}
          </p>
        )}
        {codebook && <VersionsPanel codebook={codebook} onRestored={onRestored} />}
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
            proposalCodeIds={proposalCodeIds}
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
            onSessionEnd={onSessionEnd}
            onNoteSaved={onNoteSaved}
            proposals={proposals[selected.id]}
            drift={driftStates[selected.id]}
            onProposalUpdated={onProposalUpdated}
            onProposalsRefresh={refreshProposals}
            onProposalDecided={onProposalDecided}
          />
        ) : (
          <p className="placeholder">Select a code</p>
        )}
      </main>
    </div>
  );
}
