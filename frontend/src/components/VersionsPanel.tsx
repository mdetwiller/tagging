import { useCallback, useEffect, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Codebook, CodebookVersion, VersionChange, VersionFieldChange } from "../lib/types";

interface Props {
  codebook: Codebook;
  /** After a restore, which can change any code. */
  onRestored: () => Promise<void>;
}

const FIELD_LABELS: Record<VersionFieldChange["field"], string> = {
  name: "name",
  definition: "definition",
  threshold: "threshold",
  parent_code_id: "parent",
  status: "status",
  rules: "include/exclude rules",
};

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function VersionsPanel({ codebook, onRestored }: Props) {
  const [versions, setVersions] = useState<CodebookVersion[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ version: CodebookVersion; changes: VersionChange[] } | null>(null);

  const codeNames = new Map(codebook.codes.flatMap((c) => [c, ...c.children]).map((c) => [c.id, c.name]));

  const load = useCallback(() => {
    api.versions().then(setVersions, (err) => setError(errorDetail(err)));
  }, []);
  useEffect(load, [load, codebook.id]);

  async function save() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await api.saveVersion(name);
      setName("");
      setMessage(`Saved "${saved.name}".`);
      load();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  async function showPreview(version: CodebookVersion) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      setPreview({ version, changes: await api.versionDiff(version.id) });
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  async function restore() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.restoreVersion(preview.version.id);
      setMessage(
        `Restored "${preview.version.name}" (${result.changed_codes} code${result.changed_codes === 1 ? "" : "s"} changed). ` +
          "The state before it was saved as a version."
      );
      setPreview(null);
      load();
      await onRestored();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  function describe(change: VersionFieldChange): string {
    const label = FIELD_LABELS[change.field];
    if (change.field === "rules" || change.field === "definition") return label;
    const show = (v: unknown) =>
      change.field === "parent_code_id"
        ? v === null
          ? "top level"
          : (codeNames.get(v as number) ?? `code ${v}`)
        : String(v);
    return `${label} ${show(change.before)} → ${show(change.after)}`;
  }

  return (
    <details className="panel versions">
      <summary>Versions{versions && versions.length > 0 ? ` (${versions.length})` : ""}</summary>
      <div className="version-save">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name this version"
          aria-label="Version name"
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim() && !busy) save();
          }}
        />
        <button onClick={save} disabled={busy || !name.trim()}>
          Save version
        </button>
      </div>
      {message && <p className="muted">{message}</p>}
      {error && <p className="error">{error}</p>}
      {preview && (
        <div className="version-preview">
          <p>
            Restore <strong>{preview.version.name}</strong>?
          </p>
          {preview.changes.length === 0 ? (
            <p className="muted">The codebook already matches this version.</p>
          ) : (
            <ul>
              {preview.changes.map((c) => (
                <li key={c.code_id}>
                  <strong>{c.name}</strong>:{" "}
                  {c.kind === "retire_added" ? "added since; will be retired" : c.changes.map(describe).join("; ")}
                </li>
              ))}
            </ul>
          )}
          <p className="muted">Your judgments are kept. The current state is saved as a version first.</p>
          <button onClick={restore} disabled={busy || preview.changes.length === 0}>
            Restore
          </button>{" "}
          <button onClick={() => setPreview(null)} disabled={busy}>
            Cancel
          </button>
        </div>
      )}
      {versions && versions.length === 0 && <p className="muted">No saved versions yet.</p>}
      {versions && versions.length > 0 && (
        <ul className="version-list">
          {versions.map((v) => (
            <li key={v.id}>
              <span>
                {v.name}
                {v.source === "before_restore" && <span className="muted"> (auto)</span>}
              </span>{" "}
              <span className="muted">{formatDate(v.created_at)}</span>{" "}
              <button onClick={() => showPreview(v)} disabled={busy}>
                Restore…
              </button>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
