import { useState } from "react";
import { api, errorDetail } from "../lib/api";
import type { Codebook, EmbeddingStatus, ImportResult } from "../lib/types";

interface Props {
  codebook: Codebook | null;
  embedding: EmbeddingStatus | null;
  onChanged: () => Promise<void>;
  /** Set once setup is complete, so the panel stays until its result has been read. */
  onClose: (() => void) | null;
}

export default function SetupPanel({ codebook, embedding, onChanged, onClose }: Props) {
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  const [confirming, setConfirming] = useState(false);
  const [embedRunning, setEmbeddingRunning] = useState(false);
  const [embedMessage, setEmbedMessage] = useState<string | null>(null);
  const [embedError, setEmbedError] = useState<string | null>(null);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setImporting(true);
    setImportError(null);
    setImportResult(null);
    try {
      setImportResult(await api.importCodebook(file));
      await onChanged();
    } catch (err) {
      setImportError(errorDetail(err));
    } finally {
      setImporting(false);
    }
  }

  async function onConfirmEmbed() {
    setEmbeddingRunning(true);
    setEmbedError(null);
    setEmbedMessage(null);
    try {
      const result = await api.embed();
      setEmbedMessage(`Embedded. Tokens used: ${result.tokens_used.toLocaleString()}.`);
      setConfirming(false);
      await onChanged();
    } catch (err) {
      setEmbedError(errorDetail(err));
    } finally {
      setEmbeddingRunning(false);
    }
  }

  const embedDone =
    embedding !== null &&
    embedding.highlights_embedded >= embedding.highlights_total &&
    embedding.stale_codes === 0;

  return (
    <section className="panel setup">
      <h2>Setup</h2>
      {onClose && (
        <p className="ok">
          Setup is complete. <button onClick={onClose}>Close setup</button>
        </p>
      )}

      <div className="step">
        <h3>1. Codebook</h3>
        <p className="muted">
          {codebook ? `Current: ${codebook.name}. Importing again creates a new codebook.` : "No codebook yet."}
        </p>
        <label className="file">
          Import reconciled JSON{" "}
          <input type="file" accept=".json,application/json" onChange={onFile} disabled={importing} />
        </label>
        {importing && <p className="muted">Importing…</p>}
        {importError && <p className="error">{importError}</p>}
        {importResult && (
          <div className="result">
            <p>
              Imported {importResult.parents} parents, {importResult.children} children and{" "}
              {importResult.exemplars} exemplars.
            </p>
            {importResult.warnings.length > 0 && (
              <>
                <p>{importResult.warnings.length} warnings:</p>
                <ul className="warnings">
                  {importResult.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </div>

      <div className="step">
        <h3>2. Embeddings</h3>
        {embedding === null ? (
          <p className="muted">Loading embedding status…</p>
        ) : (
          <p className="muted">
            {embedding.highlights_embedded.toLocaleString()} of {embedding.highlights_total.toLocaleString()} highlights
            embedded; {embedding.stale_codes} codes need a new query embedding.
          </p>
        )}
        {!embedDone && !confirming && (
          <button
            onClick={() => {
              setConfirming(true);
              setEmbedError(null);
              setEmbedMessage(null);
            }}
            disabled={embedding === null || codebook === null}
          >
            Embed highlights
          </button>
        )}
        {codebook === null && <p className="muted">Import a codebook first.</p>}
        {confirming && embedding && (
          <div className="confirm">
            <p>
              This is a paid OpenAI call. Estimated {embedding.est_tokens.toLocaleString()} tokens, about $
              {embedding.est_usd.toFixed(4)}.
            </p>
            <button onClick={onConfirmEmbed} disabled={embedRunning}>
              {embedRunning ? "Embedding…" : "Confirm"}
            </button>{" "}
            <button onClick={() => setConfirming(false)} disabled={embedRunning}>
              Cancel
            </button>
          </div>
        )}
        {embedMessage && <p className="ok">{embedMessage}</p>}
        {embedError && <p className="error">{embedError}</p>}
      </div>
    </section>
  );
}
