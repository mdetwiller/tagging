import { useEffect, useState } from "react";
import { api, errorDetail, errorStatus } from "../lib/api";
import type {
  Agreement,
  CodeValues,
  Proposal,
  ProposalPart,
  ProposalScoreSummary,
  ProposalStatus,
  ScoreStatus,
} from "../lib/types";

/** The drift check started when a review session ended, kept by App per code. */
export type DriftState =
  | { kind: "drafting" }
  | { kind: "no_change" }
  | { kind: "error"; message: string };

interface Props {
  /** Newest first; undefined until loaded. */
  proposals: Proposal[] | undefined;
  drift: DriftState | undefined;
  /** Replaces one proposal in App's map (after a poll or a score start). */
  onUpdated: (proposal: Proposal) => void;
  /** Refreshes this code's proposals. */
  onRefresh: () => Promise<void>;
  /** After accept or rollback: refreshes the codebook, embedding status and proposals. */
  onDecided: () => Promise<void>;
  /** Marks the proposal's disagreements in the review and scrolls to the first one. */
  onShowDisagreements: (highlightIds: number[]) => void;
}

const POLL_MS = 2000;

const STATUS_LABELS: Record<ProposalStatus, string> = {
  suggested: "suggested",
  accepted: "accepted",
  dismissed: "dismissed",
  rolled_back: "rolled back",
  no_change: "no change",
  failed: "failed",
};

function partsOf(p: Proposal): ProposalPart[] {
  const parts: ProposalPart[] = [];
  if (p.proposed.name !== undefined) parts.push("name");
  if (p.proposed.definition !== undefined) parts.push("definition");
  if (p.proposed.threshold !== undefined) parts.push("threshold");
  if (p.proposed.include !== undefined || p.proposed.exclude !== undefined) parts.push("rules");
  return parts;
}

const PART_FIELDS: Record<ProposalPart, (keyof CodeValues)[]> = {
  name: ["name"],
  definition: ["definition"],
  threshold: ["threshold"],
  rules: ["include", "exclude"],
};

function shorten(text: string, max = 60): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function describeRules(include: string[], exclude: string[]): string {
  const list = (rules: string[], kind: string) =>
    rules.length === 0
      ? `no ${kind} rules`
      : rules.length <= 2
        ? `${kind} "${rules.map((r) => shorten(r, 40)).join('", "')}"`
        : `${rules.length} ${kind} rules`;
  return `${list(include, "include")}, ${list(exclude, "exclude")}`;
}

/** "<part>: <applied> → <base>" for each part the proposal applied, i.e. what a rollback restores. */
function rollbackChanges(p: Proposal): string[] {
  const applied = p.applied ?? {};
  const lines: string[] = [];
  for (const part of Object.keys(PART_FIELDS) as ProposalPart[]) {
    if (!PART_FIELDS[part].every((f) => applied[f] !== undefined)) continue;
    if (part === "rules") {
      lines.push(
        `rules: ${describeRules(applied.include ?? [], applied.exclude ?? [])} → ${describeRules(p.base.include, p.base.exclude)}`
      );
    } else if (part === "threshold") {
      lines.push(`threshold: ${applied.threshold} → ${p.base.threshold}`);
    } else {
      lines.push(`${part}: "${shorten(String(applied[part]))}" → "${shorten(String(p.base[part]))}"`);
    }
  }
  return lines;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function formatPercent(p: number | null | undefined): string {
  return p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`;
}

function formatKappa(k: number | null | undefined): string {
  return k === null || k === undefined ? "—" : k.toFixed(2);
}

function parseLines(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l !== "");
}

function RuleList({ rules }: { rules: string[] }) {
  if (rules.length === 0) return <p className="muted">(none)</p>;
  return (
    <ul className="rule-list">
      {rules.map((r, i) => (
        <li key={i}>{r}</li>
      ))}
    </ul>
  );
}

export default function ProposalPanel({ proposals, drift, onUpdated, onRefresh, onDecided, onShowDisagreements }: Props) {
  const [notice, setNotice] = useState<string | null>(null);
  const suggested = proposals?.find((p) => p.status === "suggested");
  const history = proposals?.filter((p) => p.status !== "suggested") ?? [];
  const newestAccepted = proposals?.find((p) => p.status === "accepted");

  return (
    <>
      {drift?.kind === "drafting" && <p className="muted">Checking this code for drift…</p>}
      {drift?.kind === "no_change" && <p className="muted">The drift check proposed no change to this code.</p>}
      {drift?.kind === "error" && <p className="error">Drift check: {drift.message}</p>}
      {notice && <p className="ok">{notice}</p>}
      {suggested && (
        <SuggestedProposal
          key={suggested.id}
          proposal={suggested}
          onUpdated={onUpdated}
          onRefresh={onRefresh}
          onDecided={onDecided}
          onNotice={setNotice}
          onShowDisagreements={onShowDisagreements}
        />
      )}
      {history.length > 0 && (
        <details className="proposal-history">
          <summary>Proposal history ({history.length})</summary>
          <ul>
            {history.map((p) => (
              <HistoryItem
                key={p.id}
                proposal={p}
                canRollBack={p.id === newestAccepted?.id}
                onDecided={onDecided}
                onNotice={setNotice}
              />
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

interface SuggestedProps {
  proposal: Proposal;
  onUpdated: (proposal: Proposal) => void;
  onRefresh: () => Promise<void>;
  onDecided: () => Promise<void>;
  onNotice: (notice: string | null) => void;
  onShowDisagreements: (highlightIds: number[]) => void;
}

function SuggestedProposal({ proposal, onUpdated, onRefresh, onDecided, onNotice, onShowDisagreements }: SuggestedProps) {
  const { base, proposed } = proposal;
  const parts = partsOf(proposal);
  const initial = {
    name: proposed.name ?? base.name,
    definition: proposed.definition ?? base.definition,
    threshold: String(proposed.threshold ?? base.threshold),
    include: (proposed.include ?? base.include).join("\n"),
    exclude: (proposed.exclude ?? base.exclude).join("\n"),
  };
  const [ticked, setTicked] = useState<Set<ProposalPart>>(() => new Set(parts));
  const [name, setName] = useState(initial.name);
  const [definition, setDefinition] = useState(initial.definition);
  const [threshold, setThreshold] = useState(initial.threshold);
  const [include, setInclude] = useState(initial.include);
  const [exclude, setExclude] = useState(initial.exclude);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scoring = proposal.score_status === "running";

  // Polls while a Jev test runs; unmounting (leaving the code) or the test ending stops it.
  useEffect(() => {
    if (!scoring) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      timer = setTimeout(() => {
        api.proposal(proposal.id).then(
          (p) => {
            if (cancelled) return;
            setError(null);
            onUpdated(p);
            if (p.score_status === "running") poll();
          },
          (err) => {
            if (cancelled) return;
            setError(errorDetail(err));
            const status = errorStatus(err);
            // A 4xx won't fix itself (the proposal is gone or the request is wrong); stop asking.
            if (status === null || status < 400 || status >= 500) poll();
          }
        );
      }, POLL_MS);
    };
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [scoring, proposal.id, onUpdated]);

  function toggle(part: ProposalPart) {
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(part)) next.delete(part);
      else next.add(part);
      return next;
    });
  }

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    onNotice(null);
    try {
      await action();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  const testWithJev = () =>
    act(async () => {
      await api.scoreProposal(proposal.id);
      onUpdated({ ...proposal, score_status: "running", score_summary: null });
      onUpdated(await api.proposal(proposal.id));
    });

  const dismiss = () =>
    act(async () => {
      await api.dismissProposal(proposal.id);
      onNotice("Proposal dismissed.");
      await onRefresh();
    });

  const accept = () =>
    act(async () => {
      const chosen = parts.filter((p) => ticked.has(p));
      const values: Partial<CodeValues> = {};
      if (ticked.has("name") && name.trim() !== initial.name.trim()) values.name = name;
      if (ticked.has("definition") && definition.trim() !== initial.definition.trim()) values.definition = definition;
      if (ticked.has("threshold") && threshold !== initial.threshold) {
        const t = Number(threshold);
        if (threshold.trim() === "" || Number.isNaN(t) || t < 0 || t > 1) {
          throw new Error("The threshold must be a number from 0 to 1.");
        }
        if (t !== Number(initial.threshold)) values.threshold = t;
      }
      if (ticked.has("rules") && (include !== initial.include || exclude !== initial.exclude)) {
        values.include = parseLines(include);
        values.exclude = parseLines(exclude);
      }
      await api.acceptProposal(proposal.id, {
        parts: chosen,
        ...(Object.keys(values).length > 0 ? { values } : {}),
      });
      onNotice(
        chosen.includes("name") || chosen.includes("definition")
          ? "Proposal accepted. The code's query needs a new embedding before Find more works for it."
          : "Proposal accepted."
      );
      await onDecided();
    });

  const n = proposal.disagreements.length;
  const summary = proposal.score_summary;
  const anyTicked = parts.some((p) => ticked.has(p));
  const stale = new Set(proposal.stale_parts ?? []);
  const anyStale = parts.some((p) => stale.has(p));

  return (
    <section className="panel proposal">
      <h3>Drift proposal</h3>
      <p className="proposal-rationale">{proposal.rationale || "(no rationale given)"}</p>
      <p className="muted">
        <button className="link" onClick={() => onShowDisagreements(proposal.disagreements)} disabled={n === 0}>
          Based on {n} disagreement{n === 1 ? "" : "s"}
        </button>
        {" · "}drafted {formatDate(proposal.created_at)}
      </p>
      <table className="proposal-parts">
        <thead>
          <tr>
            <th>Accept</th>
            <th>Current</th>
            <th>Proposed</th>
          </tr>
        </thead>
        <tbody>
          {parts.includes("name") && (
            <PartRow part="name" stale={stale} label="Name" ticked={ticked} onToggle={toggle} current={base.name}>
              <input value={name} onChange={(e) => setName(e.target.value)} disabled={!ticked.has("name")} />
            </PartRow>
          )}
          {parts.includes("definition") && (
            <PartRow part="definition" stale={stale} label="Definition" ticked={ticked} onToggle={toggle} current={base.definition}>
              <textarea
                rows={4}
                value={definition}
                onChange={(e) => setDefinition(e.target.value)}
                disabled={!ticked.has("definition")}
              />
            </PartRow>
          )}
          {parts.includes("threshold") && (
            <PartRow part="threshold" stale={stale} label="Threshold" ticked={ticked} onToggle={toggle} current={String(base.threshold)}>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
                disabled={!ticked.has("threshold")}
              />
            </PartRow>
          )}
          {parts.includes("rules") && (
            <PartRow
              part="rules"
              stale={stale}
              label="Rules"
              ticked={ticked}
              onToggle={toggle}
              current={
                <>
                  <strong>Include</strong>
                  <RuleList rules={base.include} />
                  <strong>Exclude</strong>
                  <RuleList rules={base.exclude} />
                </>
              }
            >
              <label>
                Include (one rule per line)
                <textarea
                  rows={4}
                  value={include}
                  onChange={(e) => setInclude(e.target.value)}
                  disabled={!ticked.has("rules")}
                />
              </label>
              <label>
                Exclude (one rule per line)
                <textarea
                  rows={4}
                  value={exclude}
                  onChange={(e) => setExclude(e.target.value)}
                  disabled={!ticked.has("rules")}
                />
              </label>
            </PartRow>
          )}
        </tbody>
      </table>
      {anyStale && (
        <p className="warn">Accepting will be refused for changed parts; dismiss and end a new session to redraft.</p>
      )}
      <div className="proposal-actions">
        <button onClick={testWithJev} disabled={busy || scoring}>
          {scoring ? "Testing with Jev…" : "Test with Jev"}
        </button>
        <button onClick={accept} disabled={busy || scoring || !anyTicked}>
          Accept selected
        </button>
        <button onClick={dismiss} disabled={busy || scoring}>
          Dismiss
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {proposal.score_status !== "none" && <ScoreTable status={proposal.score_status} summary={summary ?? {}} />}
    </section>
  );
}

interface PartRowProps {
  part: ProposalPart;
  /** Parts changed on the code since the proposal was drafted. */
  stale: Set<ProposalPart>;
  label: string;
  ticked: Set<ProposalPart>;
  onToggle: (part: ProposalPart) => void;
  current: React.ReactNode;
  children: React.ReactNode;
}

function PartRow({ part, stale, label, ticked, onToggle, current, children }: PartRowProps) {
  return (
    <tr>
      <td>
        <label className="part-tick">
          <input type="checkbox" checked={ticked.has(part)} onChange={() => onToggle(part)} /> {label}
        </label>
        {stale.has(part) && <p className="warn">changed since drafted</p>}
      </td>
      <td className="part-current">{current}</td>
      <td className="part-proposed">{children}</td>
    </tr>
  );
}

function ScoreTable({ status, summary }: { status: ScoreStatus; summary: ProposalScoreSummary }) {
  const rows: { label: string; value: (a: Agreement | undefined) => string }[] = [
    { label: "n", value: (a) => (a ? String(a.n) : "—") },
    { label: "Percent match", value: (a) => formatPercent(a?.percent_match) },
    { label: "Kappa", value: (a) => formatKappa(a?.kappa) },
  ];
  return (
    <div className="score">
      {status === "running" && <p className="muted">Testing with Jev… {summary.after?.n ?? 0} highlights scored so far.</p>}
      {status === "done" && <p className="muted">Jev test against the highlights you judged:</p>}
      {(summary.before || summary.after) && (
        <table className="score-table">
          <thead>
            <tr>
              <th />
              <th>Current</th>
              <th>Proposed</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <th>{r.label}</th>
                <td>{r.value(summary.before)}</td>
                <td>{r.value(summary.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(summary.failed_requests ?? 0) > 0 && (
        <p className="warn">{summary.failed_requests} Jev requests failed; those highlights were skipped.</p>
      )}
      {(summary.missing_answers ?? 0) > 0 && <p className="warn">{summary.missing_answers} answers were missing.</p>}
      {status === "failed" && <p className="error">The Jev test failed{summary.error ? `: ${summary.error}` : "."}</p>}
    </div>
  );
}

interface HistoryItemProps {
  proposal: Proposal;
  canRollBack: boolean;
  onDecided: () => Promise<void>;
  onNotice: (notice: string | null) => void;
}

function HistoryItem({ proposal, canRollBack, onDecided, onNotice }: HistoryItemProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const firstLine = proposal.rationale.split("\n")[0] || proposal.ai_metadata?.error || "";

  async function rollBack() {
    setBusy(true);
    setError(null);
    onNotice(null);
    try {
      await api.rollbackProposal(proposal.id);
      setConfirming(false);
      const applied = proposal.applied ?? {};
      onNotice(
        applied.name !== undefined || applied.definition !== undefined
          ? "Proposal rolled back. The code's query needs a new embedding before Find more works for it."
          : "Proposal rolled back."
      );
      await onDecided();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="proposal-history-item">
      <span className={`proposal-status status-${proposal.status}`}>{STATUS_LABELS[proposal.status]}</span>{" "}
      <span className="muted">{formatDate(proposal.created_at)}</span> {firstLine}
      {canRollBack && !confirming && (
        <>
          {" "}
          <button onClick={() => setConfirming(true)} disabled={busy}>
            Roll back
          </button>
        </>
      )}
      {confirming && (
        <div className="confirm">
          Restore the code's values from before this proposal was accepted?
          <ul>
            {rollbackChanges(proposal).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <button onClick={rollBack} disabled={busy}>
            {busy ? "Rolling back…" : "Roll back"}
          </button>{" "}
          <button onClick={() => setConfirming(false)} disabled={busy}>
            Cancel
          </button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </li>
  );
}
