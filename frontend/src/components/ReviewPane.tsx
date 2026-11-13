import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Codebook, CodeOut, ReviewData, Row } from "../lib/types";
import BulkBar from "./BulkBar";

/** Leaving a code's view with at least this many new judgments ends the review session. */
export const SESSION_END_MIN_JUDGMENTS = 5;

interface Props {
  codeId: number;
  /** The current codebook; a new object (after an edit) triggers a refetch. */
  codebook: Codebook;
  activeCodes: CodeOut[];
  /** Bumped when a run finishes, so the review data is refetched. */
  refreshKey: number;
  /** Called after any judgment or status change, so the code list can refresh agreement. */
  onChanged: () => void;
  /** Ends a review session: "Done reviewing", or leaving the code after enough new judgments. */
  onSessionEnd: (codeId: number, judgments: number) => void;
}

function formatPercent(p: number | null): string {
  return p === null ? "—" : `${Math.round(p * 100)}%`;
}

function formatNumber(x: number | null, digits = 2): string {
  return x === null ? "—" : x.toFixed(digits);
}

export default function ReviewPane({ codeId, codebook, activeCodes, refreshKey, onChanged, onSessionEnd }: Props) {
  const [data, setData] = useState<ReviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [sessionCount, setSessionCount] = useState(0);

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

  const load = useCallback(async () => {
    try {
      setData(await api.review(codeId));
      setError(null);
    } catch (err) {
      setError(errorDetail(err));
    }
  }, [codeId]);

  useEffect(() => {
    let cancelled = false;
    api.review(codeId).then(
      (d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      },
      (err) => {
        if (!cancelled) setError(errorDetail(err));
      }
    );
    return () => {
      cancelled = true;
    };
  }, [codeId, codebook, refreshKey]);

  const afterChange = useCallback(
    async (judged: number) => {
      if (judged > 0) setSessionCount((n) => n + judged);
      await load();
      onChanged();
    },
    [load, onChanged]
  );

  async function afterBulk(judged: number) {
    await afterChange(judged);
    setSelected(new Set());
  }

  function endSession() {
    onSessionEnd(codeId, sessionCount);
    setSessionCount(0);
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
  const otherCodes = activeCodes.filter((c) => c.id !== codeId);
  const rowProps = { codeId, otherCodes, selected, onToggle: toggle, onJudged: afterChange };

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
            <span>{data.run_id === null ? "No run yet" : `Run #${data.run_id}`}</span>
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
  onJudged: (judged: number) => Promise<void>;
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
  onJudged: (judged: number) => Promise<void>;
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
      await onJudged(1);
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
