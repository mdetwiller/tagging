import { useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { CodeOut, HighlightReviewStatus } from "../lib/types";

interface Props {
  codeId: number;
  selectedIds: number[];
  activeCodes: CodeOut[];
  /** Called with the number of highlights judged (0 for status changes) after an action succeeds. */
  onDone: (judged: number) => Promise<void>;
  onClear: () => void;
}

const STATUSES: HighlightReviewStatus[] = ["not_started", "in_progress", "completed"];

export default function BulkBar({ codeId, selectedIds, activeCodes, onDone, onClear }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>, judged: boolean) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onDone(judged ? selectedIds.length : 0);
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  const judge = (targetCodeId: number, applied: boolean) =>
    run(() => api.judge({ code_id: targetCodeId, highlight_ids: selectedIds, applied }), true);
  const setStatus = (status: HighlightReviewStatus) =>
    run(() => api.setHighlightStatus(selectedIds, status), false);

  return (
    <div className="bulk-bar">
      <strong>{selectedIds.length} selected</strong>
      <button onClick={() => judge(codeId, true)} disabled={busy}>
        Accept
      </button>
      <button onClick={() => judge(codeId, false)} disabled={busy}>
        Reject
      </button>
      <CodeMenu label="Apply Code" codes={activeCodes} disabled={busy} onPick={(id) => judge(id, true)} />
      <CodeMenu label="Remove Code" codes={activeCodes} disabled={busy} onPick={(id) => judge(id, false)} />
      <select
        value=""
        disabled={busy}
        onChange={(e) => {
          if (e.target.value) setStatus(e.target.value as HighlightReviewStatus);
        }}
        aria-label="Update Status"
      >
        <option value="">Update Status ▾</option>
        {STATUSES.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <button onClick={() => setStatus("not_started")} disabled={busy}>
        Mark Not Done
      </button>
      <button onClick={onClear} disabled={busy}>
        Clear
      </button>
      {error && <span className="error">{error}</span>}
    </div>
  );
}

interface MenuProps {
  label: string;
  codes: CodeOut[];
  disabled: boolean;
  onPick: (codeId: number) => void;
}

function CodeMenu({ label, codes, disabled, onPick }: MenuProps) {
  return (
    <select
      value=""
      disabled={disabled}
      onChange={(e) => {
        if (e.target.value) onPick(Number(e.target.value));
      }}
      aria-label={label}
    >
      <option value="">{label} ▾</option>
      {codes.map((c) => (
        <option key={c.id} value={String(c.id)}>
          {c.parent_code_id === null ? c.name : `  ${c.name}`}
        </option>
      ))}
    </select>
  );
}
