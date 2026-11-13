import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Codebook, CodeOut, ReviewData, Row, RowSource, RunStatus } from "../lib/types";
import BulkBar from "./BulkBar";
import FamilyNote from "./FamilyNote";

/** Leaving a code's view with at least this many new judgments ends the review session. */
export const SESSION_END_MIN_JUDGMENTS = 5;

interface Props {
  codeId: number;
  /** The current codebook; a new object (after an edit) triggers a refetch. */
  codebook: Codebook;
  activeCodes: CodeOut[];
  /** Bumped when a run finishes, so the review data is refetched. */
  refreshKey: number;
  /** The newest run, polled by App while it runs. */
  run: RunStatus | null;
  /** Starts a find-more run for the code; throws the api error (e.g. a 409). */
  onFindMore: (codeId: number) => Promise<void>;
  /** Called after any judgment or status change, so the code list can refresh agreement. */
  onChanged: () => void;
  /** Ends a review session: "Done reviewing", or leaving the code after enough new judgments. */
  onSessionEnd: (codeId: number, judgments: number) => void;
  onNoteSaved: (codeId: number, text: string) => void;
}

export interface Judged {
  codeId: number;
  highlightIds: number[];
}

function formatPercent(p: number | null): string {
  return p === null ? "—" : `${Math.round(p * 100)}%`;
}

const SOURCE_LABELS: Record<RowSource, { label: string; title: string }> = {
  sample: { label: "sample", title: "From the latest sample run" },
  search: { label: "search", title: "A similarity pick from Find more" },
  random: { label: "random", title: "A random pick from Find more" },
};

function formatNumber(x: number | null, digits = 2): string {
  return x === null ? "—" : x.toFixed(digits);
}

export default function ReviewPane({
  codeId,
  codebook,
  activeCodes,
  refreshKey,
  run,
  onFindMore,
  onChanged,
  onSessionEnd,
  onNoteSaved,
}: Props) {
  const [data, setData] = useState<ReviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [findMoreError, setFindMoreError] = useState<string | null>(null);
  const [startingFindMore, setStartingFindMore] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Distinct highlights judged for this code (not other codes) since it was opened.
  const [sessionJudged, setSessionJudged] = useState<Set<number>>(new Set());
  const sessionCount = sessionJudged.size;
  const [noteOpen, setNoteOpen] = useState(false);
  // The note stays mounted once opened, so a save still in flight when collapsed can report.
  const [noteMounted, setNoteMounted] = useState(false);

  // App remounts this component per code (key={codeId}), so unmounting means leaving the code.
  const sessionRef = useRef({ codeId, count: 0, onSessionEnd });
  useEffect(() => {
    sessionRef.current = { codeId, count: sessionCount, onSessionEnd };
  }, [codeId, sessionCount, onSessionEnd]);
  useEffect(() => {
    const session = sessionRef;
    return () => {
      const { codeId: id, count, onSessionEnd: end } = session.current;
      if (count >= SESSION_END_MIN_JUDGMENTS) end(id, count);
    };
  }, []);

  // Only the newest review request may set data, so a slow older response can't overwrite it.
  const requestRef = useRef(0);
  const fetchReview = useCallback(() => {
    const request = ++requestRef.current;
    return api.review(codeId).then(
      (d) => {
        if (request !== requestRef.current) return;
        setData(d);
        setError(null);
        const shown = new Set([...d.found, ...d.below_threshold, ...d.random].map((r) => r.highlight_id));
        setSelected((prev) => {
          const kept = [...prev].filter((id) => shown.has(id));
          return kept.length === prev.size ? prev : new Set(kept);
        });
      },
      (err) => {
        if (request === requestRef.current) setError(errorDetail(err));
      }
    );
  }, [codeId]);

  useEffect(() => {
    const requests = requestRef;
    fetchReview();
    return () => {
      requests.current++;
    };
  }, [fetchReview, codebook, refreshKey]);

  /** `judged` is the code and highlights of a judgment just written, or null for a status change. */
  const afterChange = useCallback(
    async (judged: Judged | null) => {
      if (judged && judged.codeId === codeId) {
        setSessionJudged((prev) => new Set([...prev, ...judged.highlightIds]));
      }
      await fetchReview();
      onChanged();
    },
    [codeId, fetchReview, onChanged]
  );

  async function afterBulk(judged: Judged | null) {
    await afterChange(judged);
    setSelected(new Set());
  }

  async function findMore() {
    setStartingFindMore(true);
    setFindMoreError(null);
    try {
      await onFindMore(codeId);
    } catch (err) {
      setFindMoreError(errorDetail(err));
    } finally {
      setStartingFindMore(false);
    }
  }

  function endSession() {
    onSessionEnd(codeId, sessionCount);
    setSessionJudged(new Set());
  }

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll(rows: Row[], on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const r of rows) {
        if (on) next.add(r.highlight_id);
        else next.delete(r.highlight_id);
      }
      return next;
    });
  }

  if (error && !data) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading review…</p>;

  const a = data.agreement;
  const anyRunning = run?.status === "running";
  const findingMore = anyRunning && run?.scope_code_id === codeId;
  const findMoreCount = data.find_more_run_ids.length;
  const otherCodes = activeCodes.filter((c) => c.id !== codeId);
  const rowProps = { codeId, otherCodes, selected, onToggle: toggle, onJudged: afterChange };
  const family = codebook.codes.find((p) => p.id === codeId || p.children.some((c) => c.id === codeId));

  return (
    <div className="review">
      <header className="review-header">
        <div>
          <h2>{data.code.name}</h2>
          <div className="review-meta">
            <span>Threshold {data.code.threshold}</span>
            <span title="Highlights with both a human and an AI judgment">
              n={a.n} · match {formatPercent(a.percent_match)} · κ {formatNumber(a.kappa)}
            </span>
            <span>{data.sample_run_id === null ? "No sample run yet" : `Sample run #${data.sample_run_id}`}</span>
            {findMoreCount > 0 && (
              <span title={`Runs ${data.find_more_run_ids.join(", ")}`}>
                {findMoreCount} find-more run{findMoreCount === 1 ? "" : "s"}
              </span>
            )}
          </div>
          <div className="find-more">
            <button
              onClick={findMore}
              disabled={startingFindMore || anyRunning}
              title={anyRunning && !findingMore ? "Another run is running" : undefined}
            >
              Find more for this code
            </button>
            {findingMore && run && (
              <span className="muted">
                {" "}
                Finding more… {run.requests} requests
                {run.summary?.codes?.[String(codeId)] && `, ${run.summary.codes[String(codeId)].positives} positives`}
              </span>
            )}
            {findMoreError && <p className="error">{findMoreError}</p>}
          </div>
        </div>
        <div className="session">
          <span title="Highlights judged since this code was opened">{sessionCount} judged this session</span>
          <button onClick={endSession} disabled={sessionCount === 0}>
            Done reviewing
          </button>
        </div>
      </header>
      {error && <p className="error">{error}</p>}
      {family && (
        <details
          className="family-note-panel"
          open={noteOpen}
          onToggle={(e) => {
            const isOpen = e.currentTarget.open;
            setNoteOpen(isOpen);
            if (isOpen) setNoteMounted(true);
          }}
        >
          <summary>Family notes: {family.name}</summary>
          {noteMounted && (
            <FamilyNote
              codeId={family.id}
              open={noteOpen}
              onSaved={(note) => onNoteSaved(note.code_id, note.text)}
            />
          )}
        </details>
      )}
      {selected.size > 0 && (
        <BulkBar
          codeId={codeId}
          selectedIds={[...selected]}
          activeCodes={activeCodes}
          onDone={afterBulk}
          onClear={() => setSelected(new Set())}
        />
      )}
      <RowList title="Found" rows={data.found} empty="Nothing found yet." onToggleAll={toggleAll} {...rowProps} />
      {data.below_threshold.length > 0 && (
        <details className="below-threshold">
          <summary>Below threshold ({data.below_threshold.length})</summary>
          <RowList title="Below threshold" rows={data.below_threshold} empty="" onToggleAll={toggleAll} {...rowProps} />
        </details>
      )}
      <div className="divider">Random sample</div>
      <RowList title="Random sample" rows={data.random} empty="No random sample." onToggleAll={toggleAll} {...rowProps} />
    </div>
  );
}

interface RowListProps {
  title: string;
  rows: Row[];
  empty: string;
  codeId: number;
  otherCodes: CodeOut[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  onToggleAll: (rows: Row[], on: boolean) => void;
  onJudged: (judged: Judged) => Promise<void>;
}

function RowList({ title, rows, empty, onToggleAll, ...rowProps }: RowListProps) {
  if (rows.length === 0) return <p className="muted">{empty}</p>;
  const allOn = rows.every((r) => rowProps.selected.has(r.highlight_id));
  return (
    <div className="row-list">
      <label className="select-all">
        <input type="checkbox" checked={allOn} onChange={(e) => onToggleAll(rows, e.target.checked)} /> Select all{" "}
        {title.toLowerCase()} ({rows.length})
      </label>
      <ul>
        {rows.map((r) => (
          <HighlightRow key={r.highlight_id} row={r} {...rowProps} />
        ))}
      </ul>
    </div>
  );
}

interface HighlightRowProps {
  row: Row;
  codeId: number;
  otherCodes: CodeOut[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  onJudged: (judged: Judged) => Promise<void>;
}

function HighlightRow({ row, codeId, otherCodes, selected, onToggle, onJudged }: HighlightRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function judge(targetCodeId: number, applied: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.judge({ code_id: targetCodeId, highlight_ids: [row.highlight_id], applied });
      setPicking(false);
      await onJudged({ codeId: targetCodeId, highlightIds: [row.highlight_id] });
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  const applied = row.other_codes.filter((c) => c.final === true);
  const appliedIds = new Set(applied.map((c) => c.code_id));
  const pickable = otherCodes.filter((c) => !appliedIds.has(c.id));
  const finalClass = row.final === true ? " final-yes" : row.final === false ? " final-no" : "";

  return (
    <li className={`hl-row${finalClass}`}>
      <input
        type="checkbox"
        checked={selected.has(row.highlight_id)}
        onChange={() => onToggle(row.highlight_id)}
        aria-label={`Select highlight ${row.highlight_id}`}
      />
      <div className="hl-body">
        <div className="hl-meta">
          <span>#{row.highlight_id}</span>
          <span>conv {row.conversation_id ?? "—"}</span>
          <span title="AI probability">AI {formatNumber(row.ai_probability)}</span>
          <span title="Your judgment">
            Human {row.human === "applied" ? "✓" : row.human === "not_applied" ? "✗" : "—"}
          </span>
          <span>{row.status}</span>
          {row.source && (
            <span className={`source source-${row.source}`} title={SOURCE_LABELS[row.source].title}>
              {SOURCE_LABELS[row.source].label}
            </span>
          )}
        </div>
        <p
          className={`hl-text${expanded ? "" : " clamped"}`}
          onClick={() => setExpanded((e) => !e)}
          title={expanded ? "Click to collapse" : "Click to expand"}
        >
          {row.text}
        </p>
        <div className="hl-codes">
          {applied.map((c) => (
            <span key={c.code_id} className="chip">
              {c.name}
              <button onClick={() => judge(c.code_id, false)} disabled={busy} aria-label={`Remove ${c.name}`}>
                ×
              </button>
            </span>
          ))}
          {picking ? (
            <select
              value=""
              disabled={busy}
              onChange={(e) => {
                if (e.target.value) judge(Number(e.target.value), true);
              }}
              onBlur={() => setPicking(false)}
              autoFocus
              aria-label="Apply another code"
            >
              <option value="">Pick a code…</option>
              {pickable.map((c) => (
                <option key={c.id} value={String(c.id)}>
                  {c.name}
                </option>
              ))}
            </select>
          ) : (
            <button className="add-code" onClick={() => setPicking(true)} disabled={busy}>
              + code
            </button>
          )}
        </div>
        {error && <p className="error">{error}</p>}
      </div>
      <div className="hl-actions">
        <button
          className={`accept${row.human === "applied" ? " on" : ""}`}
          onClick={() => judge(codeId, true)}
          disabled={busy}
          title="Accept: this code applies"
        >
          ✓
        </button>
        <button
          className={`reject${row.human === "not_applied" ? " on" : ""}`}
          onClick={() => judge(codeId, false)}
          disabled={busy}
          title="Reject: this code does not apply"
        >
          ✗
        </button>
      </div>
    </li>
  );
}
