from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Code, Codebook, CodebookVersion, CodeRevision, CodeRules
from . import rules
from .codebook_edit import CodeEditError

# Restored in this order, each with a code_revision row, like a hand edit.
FIELDS = ("name", "definition", "threshold", "parent_code_id", "status")
_QUERY_FIELDS = ("name", "definition")


class NoCodebook(LookupError):
    pass


class VersionNotFound(LookupError):
    pass


class VersionConflict(RuntimeError):
    pass


def current_codebook(db: Session) -> Codebook:
    codebook = db.scalars(select(Codebook).order_by(Codebook.id.desc()).limit(1)).first()
    if codebook is None:
        raise NoCodebook("No codebook has been imported yet.")
    return codebook


def _codes(db: Session, codebook_id: int) -> list[Code]:
    return list(db.scalars(select(Code).where(Code.codebook_id == codebook_id).order_by(Code.display_order, Code.id)))


def snapshot(db: Session, codebook_id: int) -> dict:
    out: dict[str, dict] = {}
    for code in _codes(db, codebook_id):
        include, exclude = rules.current_rules(db, code.id)
        out[str(code.id)] = {
            **{f: getattr(code, f) for f in FIELDS},
            "include": include,
            "exclude": exclude,
        }
    return {"codes": out}


def save_version(
    db: Session, name: str, source: str = "user", restored_version_id: int | None = None
) -> CodebookVersion:
    name = name.strip()
    if not name:
        raise CodeEditError("A version name cannot be blank.")
    codebook = current_codebook(db)
    version = CodebookVersion(
        codebook_id=codebook.id, name=name, snapshot=snapshot(db, codebook.id), source=source,
        restored_version_id=restored_version_id, created_by=get_config().user_id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def list_versions(db: Session) -> list[CodebookVersion]:
    """The current codebook's versions, newest first."""
    codebook = current_codebook(db)
    return list(
        db.scalars(
            select(CodebookVersion)
            .where(CodebookVersion.codebook_id == codebook.id)
            .order_by(CodebookVersion.id.desc())
        )
    )


def _get(db: Session, version_id: int) -> CodebookVersion:
    version = db.get(CodebookVersion, version_id)
    if version is None:
        raise VersionNotFound(f"Version {version_id} not found.")
    if version.codebook_id != current_codebook(db).id:
        raise VersionConflict("This version belongs to an older codebook; only the current codebook can be restored.")
    return version


def diff(db: Session, version_id: int) -> list[dict]:
    """What restoring the version would change, one entry per code that changes:
    {code_id, name, kind: "edit" | "retire_added", changes: [{field, before, after}]}.
    `before` is the current value, `after` the version's. Rules are one field, "rules"."""
    version = _get(db, version_id)
    saved: dict[str, dict] = version.snapshot["codes"]
    out = []
    for code in _codes(db, version.codebook_id):
        old = saved.get(str(code.id))
        if old is None:
            if code.status != "RETIRED":
                out.append({
                    "code_id": code.id, "name": code.name, "kind": "retire_added",
                    "changes": [{"field": "status", "before": code.status, "after": "RETIRED"}],
                })
            continue
        changes: list[dict[str, Any]] = [
            {"field": f, "before": getattr(code, f), "after": old[f]} for f in FIELDS if getattr(code, f) != old[f]
        ]
        include, exclude = rules.current_rules(db, code.id)
        if (include, exclude) != (old["include"], old["exclude"]):
            changes.append({
                "field": "rules",
                "before": {"include": include, "exclude": exclude},
                "after": {"include": old["include"], "exclude": old["exclude"]},
            })
        if changes:
            out.append({"code_id": code.id, "name": code.name, "kind": "edit", "changes": changes})
    return out


def restore(db: Session, version_id: int) -> dict:
    """Saves the current state as a "before_restore" version, then puts every code back as the
    version has it; codes added since are retired. Judgments are untouched. Returns
    {"saved_version_id", "changed_codes"}."""
    version = _get(db, version_id)
    changes = diff(db, version_id)
    before = save_version(db, f"Before restoring \"{version.name}\"", source="before_restore", restored_version_id=version.id)
    user_id = get_config().user_id
    for entry in changes:
        code = db.get(Code, entry["code_id"])
        for change in entry["changes"]:
            field, after = change["field"], change["after"]
            if field == "rules":
                db.add(
                    CodeRules(code_id=code.id, include=after["include"], exclude=after["exclude"], source="restore")
                )
                continue
            db.add(CodeRevision(code_id=code.id, field=field, before=getattr(code, field), after=after, created_by=user_id))
            setattr(code, field, after)
            if field in _QUERY_FIELDS:
                code.query_stale = True
    db.commit()
    return {"saved_version_id": before.id, "changed_codes": len(changes)}


def add_code(db: Session, name: str, definition: str, parent_code_id: int | None) -> Code:
    """A new active code in the current codebook, last in its level. Its query is stale until
    embedded. Writes a code_revision row with field "created"."""
    name = name.strip()
    if not name:
        raise CodeEditError("A code name cannot be blank.")
    codebook = current_codebook(db)
    if parent_code_id is not None:
        parent = db.get(Code, parent_code_id)
        if parent is None or parent.codebook_id != codebook.id:
            raise CodeEditError("The parent must be a code in the current codebook.")
        if parent.parent_code_id is not None:
            raise CodeEditError("Codes have two levels at most; the parent is itself a child.")
    last = db.scalar(select(func.max(Code.display_order)).where(Code.codebook_id == codebook.id)) or 0
    code = Code(
        codebook_id=codebook.id, parent_code_id=parent_code_id, name=name, definition=definition,
        display_order=last + 1, source="user", query_stale=True,
    )
    db.add(code)
    db.flush()
    db.add(
        CodeRevision(
            code_id=code.id, field="created", before=None,
            after={"name": name, "definition": definition, "parent_code_id": parent_code_id},
            created_by=get_config().user_id,
        )
    )
    db.commit()
    db.refresh(code)
    return code
