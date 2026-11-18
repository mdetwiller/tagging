import { useEffect, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Agreement, Codebook, CodeEdit, CodeOut, RunCodeProgress, RunOverview, RunStatus } from "../lib/types";
import FamilyNote from "./FamilyNote";

interface Props {
  codebook: Codebook;
  /** The newest run, polled while it runs, so a find-more run's counts show before the
   * overview refreshes. A running sample shows only in RunControls. */
  run: RunStatus | null;
  overview: RunOverview | null;
  selectedCodeId: number | null;
  onSelect: (id: number) => void;
  onEdited: () => Promise<void>;
  /** Bumped when a run finishes, so agreement is refetched. */
  agreementKey: number;
  /** Notes saved this page load, by parent code id: true when not empty. Overrides `has_note`. */
  noteFlags: Record<number, boolean>;
  onNoteSaved: (codeId: number, text: string) => void;
  /** Codes with an open (`suggested`) drift proposal, among those whose proposals are loaded. */
  proposalCodeIds: Set<number>;
}

function allCodes(codes: CodeOut[]): CodeOut[] {
  return codes.flatMap((c) => [c, ...c.children]);
}

/** The newer of the overview's run and the live run, when the live run qualifies. */
function newer(saved: RunStatus | null | undefined, live: RunStatus | null, qualifies: boolean): RunStatus | null {
  if (live && qualifies && (!saved || live.id >= saved.id)) return live;
  return saved ?? null;
}

interface CodeProgress {
  sample: RunCodeProgress | undefined;
  /** The latest find-more result for the code, only when newer than the latest sample run. */
  findMore: RunCodeProgress | undefined;
}

export default function CodeList({
  codebook,
  run,
  overview,
  selectedCodeId,
  onSelect,
  onEdited,
  agreementKey,
  noteFlags,
  onNoteSaved,
  proposalCodeIds,
}: Props) {
  const [agreements, setAgreements] = useState<Record<number, Agreement>>({});
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [notesOpen, setNotesOpen] = useState<Set<number>>(new Set());
  // Notes stay mounted once opened, so a save still in flight when collapsed can report.
  const [notesMounted, setNotesMounted] = useState<Set<number>>(new Set());

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

  function progressFor(codeId: number): CodeProgress {
    const key = String(codeId);
    const sample = overview?.samples[key];
    const findMore = newer(overview?.find_more[key], run, run?.scope_code_id === codeId);
    const findMoreNewer = findMore && (!sample || findMore.id > sample.run_id);
    return {
      sample,
      findMore: findMoreNewer ? findMore.summary?.codes?.[key] : undefined,
    };
  }
  const parents = codebook.codes;

  function toggleNotes(id: number) {
    setNotesMounted((prev) => (prev.has(id) ? prev : new Set([...prev, id])));
    setNotesOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

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
    progress: progressFor(code.id),
    agreement: agreements[code.id],
    selected: code.id === selectedCodeId,
    hasProposal: proposalCodeIds.has(code.id),
    onSelect,
    onEdited,
  });

  return (
    <section className="panel codes">
      <h2>{codebook.name}</h2>
      <NewCodeForm parentId={null} label="+ code" onAdded={onEdited} />
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
            <div className="family-note-toggle">
              <button
                onClick={() => toggleNotes(parent.id)}
                aria-expanded={notesOpen.has(parent.id)}
                title="Codes that may be missing from this family"
              >
                {notesOpen.has(parent.id) ? "▾" : "▸"} Notes
                {(noteFlags[parent.id] ?? parent.has_note) && (
                  <span className="note-dot" aria-label="Has a note" />
                )}
              </button>
              {notesMounted.has(parent.id) && (
                <FamilyNote
                  codeId={parent.id}
                  open={notesOpen.has(parent.id)}
                  onSaved={(note) => onNoteSaved(note.code_id, note.text)}
                />
              )}
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
            {!collapsed.has(parent.id) && (
              <div className="children">
                <NewCodeForm parentId={parent.id} label="+ child" onAdded={onEdited} />
              </div>
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
  progress: CodeProgress;
  agreement: Agreement | undefined;
  selected: boolean;
  hasProposal: boolean;
  onSelect: (id: number) => void;
  onEdited: () => Promise<void>;
}

const TOP_LEVEL = "top";

function CodeRow({ code, parents, progress, agreement, selected, hasProposal, onSelect, onEdited }: RowProps) {
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
        {hasProposal && (
          <span className="proposal-marker" title="Has a drift proposal to review">
            proposal
          </span>
        )}
        <span className="stats">
          {progress.sample && (
            <span title="Positives / highlights judged in the current sample run">
              {progress.sample.positives}/{progress.sample.tries}
              {progress.sample.few && (
                <span className="warn" title="Few positives in the sample">
                  {" "}
                  ⚠
                </span>
              )}
            </span>
          )}
          {progress.findMore && (
            <span title="Positives / tries in the latest Find more run, newer than the sample">
              {" "}
              +{progress.findMore.positives}/{progress.findMore.tries}
              {progress.findMore.hard_to_find && (
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

function NewCodeForm({
  parentId,
  label,
  onAdded,
}: {
  parentId: number | null;
  label: string;
  onAdded: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [definition, setDefinition] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add() {
    setSaving(true);
    setError(null);
    try {
      await api.addCode({ name, definition, parent_code_id: parentId });
      setName("");
      setDefinition("");
      setOpen(false);
      await onAdded();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button className="add-code-button" onClick={() => setOpen(true)}>
        {label}
      </button>
    );
  }
  return (
    <div className="code-editor new-code">
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </label>
      <label>
        Definition
        <textarea rows={3} value={definition} onChange={(e) => setDefinition(e.target.value)} />
      </label>
      <p className="muted">Find more works for it once its query is embedded (Update embeddings).</p>
      <div>
        <button onClick={add} disabled={saving || !name.trim()}>
          {saving ? "Adding…" : "Add"}
        </button>{" "}
        <button onClick={() => setOpen(false)} disabled={saving}>
          Cancel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
