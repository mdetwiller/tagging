from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy.orm import Session

from ..models import Code, CodeExemplar, Codebook


class ImportResult(TypedDict):
    codebook_id: int
    parents: int
    children: int
    exemplars: int
    warnings: list[str]


class CodebookFormatError(ValueError):
    """The uploaded file is not a Codestormer reconciled codebook."""


def _validate(data: Any) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("codes"), list):
        raise CodebookFormatError(
            "Not a Codestormer reconciled codebook: expected an object with a 'codes' list."
        )
    codes = data["codes"]
    # Cortico's export is a flat list linked by parent_id; Codestormer nests children.
    if any(isinstance(c, dict) and "parent_id" in c for c in codes):
        raise CodebookFormatError(
            "This looks like a Cortico-format export (codes with parent_id). "
            "Import Codestormer's reconciled_codebook.json instead."
        )
    for c in codes:
        if not isinstance(c, dict) or not c.get("id") or not c.get("name"):
            raise CodebookFormatError("Every code needs an 'id' and a 'name'.")
        for child in c.get("children") or []:
            if not isinstance(child, dict) or not child.get("id") or not child.get("name"):
                raise CodebookFormatError("Every code needs an 'id' and a 'name'.")
    return codes


def _x_source(code: dict) -> dict:
    # merged_from holds whole copies of the merged codes; keep only their names.
    return {
        "source": code.get("source"),
        "rq_provenance": code.get("rq_provenance"),
        "merged_from": [m.get("name") for m in code.get("merged_from") or [] if isinstance(m, dict)],
        "codestormer_origin_id": code.get("origin_id"),
        "generated": code.get("generated"),
        "name_warnings": code.get("name_warnings"),
    }


def _add_code(
    db: Session,
    codebook_id: int,
    code: dict,
    parent_id: int | None,
    order: int,
    warnings: list[str],
) -> tuple[Code, int]:
    definition = code.get("definition") or ""
    if not definition.strip():
        warnings.append(f"Code '{code['name']}' has an empty definition.")
    row = Code(
        codebook_id=codebook_id,
        parent_code_id=parent_id,
        name=code["name"],
        definition=definition,
        display_order=order,
        origin_id=str(code["id"]),
        source="codestormer",
        x_source=_x_source(code),
        query_stale=True,
    )
    db.add(row)
    db.flush()

    # Only the code's own exemplars; highlights are Codestormer evidence, not exemplars.
    exemplar_count = 0
    for entry in code.get("exemplars") or []:
        for turn in entry.get("evidence") or []:
            text = turn.get("span_text") or turn.get("text") or ""
            if not text.strip():
                warnings.append(
                    f"Code '{code['name']}': skipped an exemplar turn with no text "
                    f"(snippet {turn.get('snippet_id')})."
                )
                continue
            db.add(
                CodeExemplar(
                    code_id=row.id,
                    snippet_id=turn.get("snippet_id"),
                    conversation_id=turn.get("conversation_id", entry.get("conversation_id")),
                    text=text,
                    source="codestormer",
                )
            )
            exemplar_count += 1
    return row, exemplar_count


def import_reconciled(db: Session, data: dict, source_file: str) -> ImportResult:
    """Import a Codestormer reconciled codebook as a new codebook. Raises CodebookFormatError."""
    codes = _validate(data)
    warnings: list[str] = []
    codebook = Codebook(
        name=f"Codestormer reconciled codebook ({data.get('last_saved') or 'undated'})",
        source_file=source_file,
        source_last_saved=data.get("last_saved"),
    )
    db.add(codebook)
    db.flush()

    parents = children = exemplars = 0
    try:
        for p_order, parent in enumerate(codes):
            parent_row, n = _add_code(db, codebook.id, parent, None, p_order, warnings)
            parents += 1
            exemplars += n
            for c_order, child in enumerate(parent.get("children") or []):
                _, n = _add_code(db, codebook.id, child, parent_row.id, c_order, warnings)
                children += 1
                exemplars += n
                if child.get("children"):
                    warnings.append(
                        f"Code '{child['name']}' has nested children; only two levels are "
                        "imported, so they were skipped."
                    )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return ImportResult(
        codebook_id=codebook.id,
        parents=parents,
        children=children,
        exemplars=exemplars,
        warnings=warnings,
    )
