from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_tagging_db
from ..models import Code, CodeExemplar, Codebook, Coding
from ..schemas import CodeEdit, FamilyNoteRequest, NewCode, NewVersion
from ..services.codebook_edit import CodeEditError, CodeNotFound, edit_code, list_revisions
from ..services.codebook_import import CodebookFormatError, import_reconciled
from ..services import versions
from ..services.export import _iso
from ..services.family_notes import NotAFamily, NoteConflict, NoteCodeNotFound, current_notes, get_note, save_note

router = APIRouter(prefix="/codebooks", tags=["codebooks"])
codes_router = APIRouter(prefix="/codes", tags=["codes"])
versions_router = APIRouter(prefix="/codebook_versions", tags=["codebook versions"])


@router.post("/import")
async def import_codebook(
    file: UploadFile = File(...), db: Session = Depends(get_tagging_db)
) -> dict:
    raw = await file.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"File is not valid JSON: {exc}") from exc
    try:
        return dict(import_reconciled(db, data, file.filename or "upload.json"))
    except CodebookFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _noted(db: Session, codes: list[Code]) -> set[int]:
    """Top-level codes among codes whose current family note is not empty."""
    ids = [c.id for c in codes if c.parent_code_id is None]
    return {code_id for code_id, note in current_notes(db, ids).items() if note.text.strip()}


def _serialize_code(
    code: Code, exemplar_counts: dict[int, int], children: list[dict], noted: set[int]
) -> dict:
    return {
        "id": code.id,
        "name": code.name,
        "definition": code.definition,
        "parent_code_id": code.parent_code_id,
        "status": code.status,
        "threshold": code.threshold,
        "display_order": code.display_order,
        "exemplar_count": exemplar_counts.get(code.id, 0),
        "has_note": code.id in noted,
        "children": children,
    }


@router.get("/current")
def current_codebook(db: Session = Depends(get_tagging_db)) -> dict:
    codebook = db.scalars(select(Codebook).order_by(Codebook.id.desc()).limit(1)).first()
    if codebook is None:
        raise HTTPException(status_code=404, detail="No codebook has been imported yet.")
    codes = list(
        db.scalars(
            select(Code)
            .where(Code.codebook_id == codebook.id)
            .order_by(Code.display_order, Code.id)
        )
    )
    exemplar_counts = dict(
        db.execute(
            select(CodeExemplar.code_id, func.count(CodeExemplar.id))
            .join(Code, Code.id == CodeExemplar.code_id)
            .where(Code.codebook_id == codebook.id)
            .group_by(CodeExemplar.code_id)
        ).all()
    )
    children_of: dict[int, list[Code]] = {}
    for code in codes:
        if code.parent_code_id is not None:
            children_of.setdefault(code.parent_code_id, []).append(code)
    noted = _noted(db, codes)
    tree = [
        _serialize_code(
            parent,
            exemplar_counts,
            [_serialize_code(c, exemplar_counts, [], noted) for c in children_of.get(parent.id, [])],
            noted,
        )
        for parent in codes
        if parent.parent_code_id is None
    ]
    human_judgments = db.scalar(
        select(func.count(Coding.id))
        .join(Code, Code.id == Coding.code_id)
        .where(Code.codebook_id == codebook.id, Coding.is_ai_generated.is_(False))
    )
    return {"id": codebook.id, "name": codebook.name, "codes": tree, "human_judgments": human_judgments or 0}


@codes_router.patch("/{code_id}")
def patch_code(code_id: int, changes: CodeEdit, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        code = edit_code(db, code_id, changes)
    except CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} not found.") from exc
    except CodeEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    children = list(
        db.scalars(
            select(Code)
            .where(Code.parent_code_id == code.id)
            .order_by(Code.display_order, Code.id)
        )
    )
    ids = [code.id] + [c.id for c in children]
    exemplar_counts = dict(
        db.execute(
            select(CodeExemplar.code_id, func.count(CodeExemplar.id))
            .where(CodeExemplar.code_id.in_(ids))
            .group_by(CodeExemplar.code_id)
        ).all()
    )
    noted = _noted(db, [code])
    return _serialize_code(
        code, exemplar_counts, [_serialize_code(c, exemplar_counts, [], noted) for c in children], noted
    )


@codes_router.get("/{code_id}/revisions")
def code_revisions(code_id: int, db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        revisions = list_revisions(db, code_id)
    except CodeNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Code {code_id} not found.") from exc
    return [
        {
            "field": r.field,
            "before": r.before,
            "after": r.after,
            "created_at": r.created_at.isoformat(),
        }
        for r in revisions
    ]


def _serialize_note(code_id: int, note) -> dict:
    return {
        "code_id": code_id,
        "text": note.text if note else "",
        "updated_at": _iso(note.created_at) if note else None,
    }


@codes_router.get("/{code_id}/note")
def family_note(code_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        return _serialize_note(code_id, get_note(db, code_id))
    except NoteCodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotAFamily as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@codes_router.put("/{code_id}/note")
def put_family_note(code_id: int, body: FamilyNoteRequest, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        note = save_note(
            db,
            code_id,
            body.text,
            check_base="base_updated_at" in body.model_fields_set,
            base_updated_at=body.base_updated_at,
        )
        return _serialize_note(code_id, note)
    except NoteCodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotAFamily as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NoteConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@codes_router.post("", status_code=201)
def add_code(body: NewCode, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        code = versions.add_code(db, body.name, body.definition, body.parent_code_id)
    except versions.NoCodebook as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CodeEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_code(code, {}, [], set())


def _serialize_version(v) -> dict:
    return {
        "id": v.id,
        "name": v.name,
        "source": v.source,
        "restored_version_id": v.restored_version_id,
        "code_count": len(v.snapshot.get("codes", {})),
        "created_at": _iso(v.created_at),
    }


def _version_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, (versions.NoCodebook, versions.VersionNotFound)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, versions.VersionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@versions_router.get("")
def list_versions(db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        return [_serialize_version(v) for v in versions.list_versions(db)]
    except versions.NoCodebook as exc:
        raise _version_errors(exc) from exc


@versions_router.post("", status_code=201)
def save_version(body: NewVersion, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        return _serialize_version(versions.save_version(db, body.name))
    except (versions.NoCodebook, CodeEditError) as exc:
        raise _version_errors(exc) from exc


@versions_router.get("/{version_id}/diff")
def version_diff(version_id: int, db: Session = Depends(get_tagging_db)) -> list[dict]:
    try:
        return versions.diff(db, version_id)
    except (versions.NoCodebook, versions.VersionNotFound, versions.VersionConflict) as exc:
        raise _version_errors(exc) from exc


@versions_router.post("/{version_id}/restore")
def restore_version(version_id: int, db: Session = Depends(get_tagging_db)) -> dict:
    try:
        return versions.restore(db, version_id)
    except (versions.NoCodebook, versions.VersionNotFound, versions.VersionConflict) as exc:
        raise _version_errors(exc) from exc
