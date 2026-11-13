import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { FamilyNote as FamilyNoteData } from "../lib/types";

interface Props {
  /** A top-level code (a code family). */
  codeId: number;
  /** Called with the note as saved, so the code list can update its marker. */
  onSaved?: (note: FamilyNoteData) => void;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A family's notepad for codes that seem to be missing. The code list and the review pane
 * both edit the same note, so it is fetched on mount and again on focus while unchanged. */
export default function FamilyNote({ codeId, onSaved }: Props) {
  const inputId = useId();
  const [text, setText] = useState("");
  // The note as the backend last returned it; null until the first load succeeds.
  const [saved, setSaved] = useState<FamilyNoteData | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savingRef = useRef(false);
  const savedTextRef = useRef("");

  const dirty = saved !== null && text !== saved.text;

  const load = useCallback(() => {
    return api.familyNote(codeId).then(
      (note) => {
        // Text typed while the request was in flight is kept.
        setText((prev) => (prev === savedTextRef.current ? note.text : prev));
        savedTextRef.current = note.text;
        setSaved(note);
        setError(null);
      },
      (err) => setError(errorDetail(err))
    );
  }, [codeId]);

  useEffect(() => {
    load();
  }, [load]);

  async function save() {
    if (!dirty || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      const note = await api.saveFamilyNote(codeId, text);
      savedTextRef.current = note.text;
      setSaved(note);
      onSaved?.(note);
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  function refreshIfClean() {
    if (saved !== null && !dirty && !savingRef.current) load();
  }

  return (
    <div className="family-note">
      <label htmlFor={inputId}>Possibly missing codes</label>
      <textarea
        id={inputId}
        rows={4}
        value={text}
        disabled={saved === null}
        placeholder={saved === null && !error ? "Loading…" : "Codes this family may be missing"}
        onChange={(e) => setText(e.target.value)}
        onFocus={refreshIfClean}
        onBlur={save}
      />
      <div className="family-note-status">
        <button onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        {dirty && <span className="warn">Unsaved changes</span>}
        {!dirty && saved?.updated_at && <span className="muted">Saved {formatTime(saved.updated_at)}</span>}
      </div>
      {error && (
        <p className="error">
          {error}
          {saved === null && (
            <>
              {" "}
              <button onClick={() => load()}>Retry</button>
            </>
          )}
        </p>
      )}
    </div>
  );
}
