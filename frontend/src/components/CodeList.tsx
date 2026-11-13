import { useEffect, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Agreement, Codebook, CodeEdit, CodeOut, RunCodeProgress, RunStatus } from "../lib/types";

interface Props {
  codebook: Codebook;
  run: RunStatus | null;
  selectedCodeId: number | null;
  onSelect: (id: number) => void;
  onEdited: () => Promise<void>;
  /** Bumped when a run finishes, so agreement is refetched. */
  agreementKey: number;
}

function allCodes(codes: CodeOut[]): CodeOut[] {
  return codes.flatMap((c) => [c, ...c.children]);
}

export default function CodeList({ codebook, run, selectedCodeId, onSelect, onEdited, agreementKey }: Props) {
  const [agreements, setAgreements] = useState<Record<number, Agreement>>({});
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    const ids = allCodes(codebook.codes).map((c) => c.id);
    Promise.allSettled(ids.map((id) => api.agreement(id))).then((results) => {
      if (cancelled) return;
      const next: Record<number, Agreement> = {};
      results.forEach((r, i) => {
        if (r.status === "fulfilled") next[ids[i]] = r.value;
      });
      setAgreements(next);
    });
    return () => {
      cancelled = true;
    };
  }, [codebook, agreementKey]);

  const progress = run?.summary?.codes ?? {};
  const parents = codebook.codes;

  function toggle(id: number) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const rowProps = (code: CodeOut) => ({
    code,
    parents,
    progress: progress[String(code.id)],
    agreement: agreements[code.id],
    selected: code.id === selectedCodeId,
    onSelect,
    onEdited,
  });

  return (
    <section className="panel codes">
      <h2>{codebook.name}</h2>
      <ul className="code-tree">
        {parents.map((parent) => (
          <li key={parent.id}>
            <div className="parent-line">
              <button
                className="toggle"
                onClick={() => toggle(parent.id)}
                disabled={parent.children.length === 0}
                aria-label={collapsed.has(parent.id) ? "Expand" : "Collapse"}
              >
                {parent.children.length === 0 ? "·" : collapsed.has(parent.id) ? "▸" : "▾"}
              </button>
              <CodeRow {...rowProps(parent)} />
            </div>
            {!collapsed.has(parent.id) && parent.children.length > 0 && (
              <ul className="children">
                {parent.children.map((child) => (
                  <li key={child.id}>
                    <CodeRow {...rowProps(child)} />
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

interface RowProps {
  code: CodeOut;
  parents: CodeOut[];
  progress: RunCodeProgress | undefined;
  agreement: Agreement | undefined;
  selected: boolean;
  onSelect: (id: number) => void;
  onEdited: () => Promise<void>;
}

const TOP_LEVEL = "top";

function CodeRow({ code, parents, progress, agreement, selected, onSelect, onEdited }: RowProps) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(code.name);
  const [definition, setDefinition] = useState(code.definition);
  const [threshold, setThreshold] = useState(String(code.threshold));
  const [parent, setParent] = useState(code.parent_code_id === null ? TOP_LEVEL : String(code.parent_code_id));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const retired = code.status === "RETIRED";

  function startEditing() {
    setName(code.name);
    setDefinition(code.definition);
    setThreshold(String(code.threshold));
    setParent(code.parent_code_id === null ? TOP_LEVEL : String(code.parent_code_id));
    setError(null);
    setEditing(true);
  }

  async function patch(changes: CodeEdit): Promise<boolean> {
    if (Object.keys(changes).length === 0) return true;
    setSaving(true);
    setError(null);
    try {
      await api.editCode(code.id, changes);
      await onEdited();
      return true;
    } catch (err) {
      setError(errorDetail(err));
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function save() {
    const changes: CodeEdit = {};
    if (name.trim() !== code.name) changes.name = name.trim();
    if (definition !== code.definition) changes.definition = definition;
    const t = Number(threshold);
    if (threshold.trim() === "" || Number.isNaN(t)) {
      setError("Threshold must be a number from 0 to 1.");
      return;
    }
    if (t !== code.threshold) changes.threshold = t;
    const parentId = parent === TOP_LEVEL ? null : Number(parent);
    if (parentId !== code.parent_code_id) changes.parent_code_id = parentId;
    if (await patch(changes)) setEditing(false);
  }

  const moveTargets = parents.filter((p) => p.id !== code.id);

  return (
    <div className={`code-row${selected ? " selected" : ""}${retired ? " retired" : ""}`}>
      <div className="code-line">
        <button className="code-name" onClick={() => onSelect(code.id)} title={code.definition}>
          {code.name}
        </button>
        <span className="stats">
          {progress && (
            <span title="positives / tries in the latest run">
              {progress.positives}/{progress.tries}
              {progress.hard_to_find && (
                <span className="warn" title="Hard to find">
                  {" "}
                  ⚠
                </span>
              )}
            </span>
          )}
          {agreement && (
            <span title="Human vs AI agreement: highlights with both judgments, percent match">
              {" "}
              n={agreement.n}
              {agreement.percent_match !== null && ` ${Math.round(agreement.percent_match * 100)}%`}
            </span>
          )}
        </span>
      </div>
      <div className="code-actions">
        {retired && <span className="muted">Retired </span>}
        {!editing && (
          <button onClick={startEditing} disabled={saving}>
            Edit
          </button>
        )}{" "}
        <button onClick={() => patch({ status: retired ? "ACTIVE" : "RETIRED" })} disabled={saving}>
          {retired ? "Unretire" : "Retire"}
        </button>
      </div>
      {editing && (
        <div className="code-editor">
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Definition
            <textarea rows={4} value={definition} onChange={(e) => setDefinition(e.target.value)} />
          </label>
          <label>
            Threshold
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </label>
          <label>
            Move to…
            <select value={parent} onChange={(e) => setParent(e.target.value)}>
              <option value={TOP_LEVEL}>Top level</option>
              {moveTargets.map((p) => (
                <option key={p.id} value={String(p.id)}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <div>
            <button onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>{" "}
            <button onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
