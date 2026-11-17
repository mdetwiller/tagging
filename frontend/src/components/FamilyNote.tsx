import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, errorDetail, errorStatus } from "../lib/api";
import type { FamilyNote as FamilyNoteData } from "../lib/types";

interface Props {
  /** A top-level code (a code family). */
  codeId: number;
  /** Hidden panels stay mounted so an in-flight save can still report its error. */
  open: boolean;
  /** Called with the note as saved, so the code list can update its marker. */
  onSaved?: (note: FamilyNoteData) => void;
}

/** Unsaved text survives unmounting (collapse, switching code, a failed save) in localStorage. */
interface Draft {
  text: string;
  /** The `updated_at` of the note the draft was based on. */
  base_updated_at: string | null;
}

const draftKey = (codeId: number) => `tagging.familyNoteDraft.${codeId}`;

function readDraft(codeId: number): Draft | null {
  try {
    const raw = localStorage.getItem(draftKey(codeId));
    if (raw === null) return null;
    const draft = JSON.parse(raw);
    return typeof draft?.text === "string" ? { text: draft.text, base_updated_at: draft.base_updated_at ?? null } : null;
  } catch {
    return null;
  }
}

function writeDraft(codeId: number, draft: Draft) {
  try {
    localStorage.setItem(draftKey(codeId), JSON.stringify(draft));
  } catch {
    // Storage unavailable; the text stays in the textarea only.
  }
}

function clearDraft(codeId: number) {
  try {
    localStorage.removeItem(draftKey(codeId));
  } catch {
    // Storage unavailable; nothing to clear.
  }
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A family's notepad for codes that seem to be missing. The code list and the review pane
 * both edit the same note: it is fetched each time it opens and on focus, and a save names
 * the version it was based on, so the backend refuses (409) to overwrite a newer note. */
export default function FamilyNote({ codeId, open, onSaved }: Props) {
  const inputId = useId();
  const [text, setText] = useState("");
  // The note as the backend last returned it; null until the first load succeeds.
  const [saved, setSaved] = useState<FamilyNoteData | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [restored, setRestored] = useState(false);
  const savingRef = useRef(false);
  const textRef = useRef("");
  const savedTextRef = useRef("");
  // The version the text in the textarea is based on; sent with each save.
  const baseRef = useRef<string | null>(null);
  const loadedRef = useRef(false);

  const dirty = saved !== null && text !== saved.text;

  function setTextBoth(value: string) {
    textRef.current = value;
    setText(value);
  }

  const load = useCallback(() => {
    return api.familyNote(codeId).then(
      (note) => {
        const prevSaved = savedTextRef.current;
        if (!loadedRef.current) {
          loadedRef.current = true;
          const draft = readDraft(codeId);
          if (draft && draft.text !== note.text) {
            textRef.current = draft.text;
            setText(draft.text);
            baseRef.current = draft.base_updated_at;
            setRestored(true);
          } else {
            if (draft) clearDraft(codeId);
            textRef.current = note.text;
            setText(note.text);
            baseRef.current = note.updated_at;
          }
        } else if (textRef.current === prevSaved) {
          // No unsaved changes: show the latest note. Unsaved text keeps its older base, so
          // saving it over a newer note is refused.
          textRef.current = note.text;
          setText(note.text);
          baseRef.current = note.updated_at;
        }
        savedTextRef.current = note.text;
        setSaved(note);
        setError(null);
      },
      (err) => setError(errorDetail(err))
    );
  }, [codeId]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  function change(value: string) {
    setTextBoth(value);
    writeDraft(codeId, { text: value, base_updated_at: baseRef.current });
  }

  async function save() {
    if (!dirty || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    const sent = text;
    try {
      const note = await api.saveFamilyNote(codeId, sent, baseRef.current);
      savedTextRef.current = note.text;
      baseRef.current = note.updated_at;
      setSaved(note);
      setConflict(false);
      setRestored(false);
      if (textRef.current === sent) clearDraft(codeId);
      else writeDraft(codeId, { text: textRef.current, base_updated_at: note.updated_at });
      onSaved?.(note);
    } catch (err) {
      setConflict(errorStatus(err) === 409);
      setError(errorDetail(err));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function loadLatest() {
    try {
      const note = await api.familyNote(codeId);
      savedTextRef.current = note.text;
      baseRef.current = note.updated_at;
      setTextBoth(note.text);
      setSaved(note);
      clearDraft(codeId);
      setConflict(false);
      setRestored(false);
      setError(null);
    } catch (err) {
      setError(errorDetail(err));
    }
  }

  function refreshIfClean() {
    if (saved !== null && !dirty && !savingRef.current) load();
  }

  return (
    <div className="family-note" hidden={!open}>
      <label htmlFor={inputId}>Possibly missing codes</label>
      <textarea
        id={inputId}
        rows={4}
        value={text}
        disabled={saved === null}
        placeholder={saved === null && !error ? "Loading…" : "Codes this family may be missing"}
        onChange={(e) => change(e.target.value)}
        onFocus={refreshIfClean}
        onBlur={save}
      />
      <div className="family-note-status">
        <button onClick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        {dirty && <span className="warn">Unsaved changes</span>}
        {dirty && restored && <span className="muted">Restored unsaved draft</span>}
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
          {conflict && (
            <>
              {" "}
              <button onClick={loadLatest}>Load latest</button>
            </>
          )}
        </p>
      )}
    </div>
  );
}
