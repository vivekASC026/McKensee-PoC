"""Layout Builder API — parse a sample, assign rules, save, and approve.

Decoupled from generation. Everything here builds and reviews layout definitions;
the generator is not touched. A saved layout is usable at once, unless a second
approver is configured (Settings › Layout approval): then only an approved
layout writes files.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from sdg.analyzer.services import layout_service as ls
from sdg.discovery.errors import plain

router = APIRouter(prefix="/layouts", tags=["layouts"])

log = logging.getLogger(__name__)

# Kept in work/ beside the folder setting, not with any one database's files:
# it stays when another database is chosen (Service.STAYS).
APPROVAL_FILE = "layout-approval.json"
# Every change of the setting, one JSON object a line: who, when, old, new.
APPROVAL_LOG = "layout-approval-log.jsonl"


def _approval_path() -> Path:
    from sdg.api.app import service

    return service.work / APPROVAL_FILE


def _setting() -> tuple[str | None, bool]:
    """The second approver named (None when nobody is), and whether the
    setting file is there but cannot be read -- then approval is required,
    since a damaged file must not switch the control off."""
    path = _approval_path()
    if not path.exists():
        return None, False
    try:
        named = json.loads(path.read_text(encoding="utf-8")).get("secondApprover")
    except (OSError, ValueError, AttributeError):
        return None, True
    if named is not None and not isinstance(named, str):
        return None, True
    return (named.strip() or None) if named else None, False


def second_approver() -> str | None:
    """Who approves layouts, when somebody does; None when nobody is named."""
    return _setting()[0]


def same_name(typed: str | None, named: str) -> bool:
    """The approver's name as typed matches the one set: spaces at either end
    ignored, letters compared lower-cased (Unicode's default mapping, as the
    screen's toLowerCase does), so the screen and the server agree."""
    return (typed or "").strip().lower() == named.strip().lower()


def approval_required() -> bool:
    """A layout writes files only once approved: with a second approver, or
    when the setting cannot be read."""
    named, unreadable = _setting()
    return named is not None or unreadable


def _approval() -> dict:
    return {"secondApprover": second_approver(), "required": approval_required()}


def _who() -> str:
    from sdg.api.app import _who as who

    return who()


def _log_setting(old: str | None, new: str | None) -> None:
    """Append a change of the approval setting to its log in the work folder."""
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "who": _who(),
             "old": old, "new": new}
    with (_approval_path().parent / APPROVAL_LOG).open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


@router.post("/parse")
async def parse(file: UploadFile = File(...)):
    """Parse an uploaded sample into fields, then auto-assign a rule to each.

    Unmatched fields come back flagged is_default=True so the review UI can show
    they carry the blank fallback rather than a real assignment.
    """
    from sdg.api.limits import MAX_SAMPLE_BYTES, sample_fits

    # One byte past the limit is enough to know it is over: nothing larger is read.
    content = await file.read(MAX_SAMPLE_BYTES + 1)
    sample_fits(content)
    try:
        parsed = ls.parse_sample(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse failed: {plain(e)}")

    # Without the rules every field would get the blank default, and a layout
    # saved from that would write blanks: said, rather than shown as assigned.
    try:
        rules = ls.get_rules()
    except Exception as e:
        log.exception("Generation rules could not be read for a sample")
        raise HTTPException(status_code=500, detail=(
            f"The generation rules could not be read, so no field was assigned. "
            f"Try again when they can be read: {plain(e)}"))
    parsed["fields"] = ls.auto_assign(parsed["fields"], rules)
    parsed["sample_name"] = file.filename
    parsed["n_default"] = sum(1 for f in parsed["fields"] if f.get("is_default"))
    return parsed


@router.get("/rules")
def rules():
    """Active generation rules, for the assignment dropdown."""
    try:
        return {"rules": ls.get_rules()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


class CreateRuleRequest(BaseModel):
    field_name: str
    field_label: str | None = None
    field_category: str = "custom"
    gen_method: str
    data_type: str = "text"
    length: int | None = None
    prefix: str | None = None
    postfix: str | None = None
    start_value: int | None = None
    domain: str | None = None
    format_pattern: str | None = None
    lookup_source: str | None = None


@router.post("/rules")
def add_rule(req: CreateRuleRequest):
    """Create a generation rule inline, so an unmatched field can be assigned.

    Returns the new rule so the client can attach it to the field immediately.
    """
    try:
        rule_id = ls.create_rule(req.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rule create failed: {plain(e)}")
    return {"id": rule_id, **req.model_dump()}


class SaveLayoutRequest(BaseModel):
    name: str
    kind: str
    delimiter: str | None = None
    fields: list[dict]
    sample_name: str | None = None
    # Only 'generation': a layout assigns generation rules to its fields.
    purpose: str = "generation"
    # Saving as a new layout must not land on an existing one: saving under a
    # taken name replaces that layout, which is right for re-reading a sample
    # and wrong for "keep a copy of this".
    as_new: bool = False


@router.post("/save")
def save(req: SaveLayoutRequest):
    """Save as pending_review. Every field already has a rule (real or default)."""
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="A layout name is required.")
    if not req.fields:
        raise HTTPException(status_code=422, detail="The layout has no fields.")
    try:
        ls.check_field_names(req.fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if req.as_new and any(
        row["name"].strip().lower() == req.name.strip().lower()
        for row in _listed(include_archived=True)
    ):
        raise HTTPException(
            status_code=409,
            detail=f"There is already a layout called “{req.name.strip()}”. "
                   f"Give the new one a name of its own.")
    try:
        saved = ls.save_layout(
            req.name.strip(), req.kind, req.delimiter, req.fields,
            req.sample_name or "", created_by=_who(), purpose=req.purpose,
        )
    except ValueError as e:
        # 409: the name belongs to something that still exists. Not a malformed
        # request, and not a server fault — a conflict the caller can resolve.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {plain(e)}")
    if req.purpose == "generation":
        saved.update(ls.alias_chosen(req.fields))
    return saved


def _listed(include_archived: bool = False) -> list[dict]:
    """Saved layouts, or the reason they can't be read -- never an empty list
    in place of a table that can't be read."""
    try:
        return ls.list_layouts(include_archived)
    except ls.LayoutsTableOutdated as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Layouts can't be read: {plain(e)}")


@router.get("")
def layouts(include_archived: bool = False):
    """Saved layouts. Archived ones are hidden unless include_archived=true.

    `approvalRequired` says whether a layout must be approved to write files."""
    return {"layouts": _listed(include_archived),
            "approvalRequired": approval_required()}


class ApprovalSetting(BaseModel):
    # Empty or missing: no second approver, and no approval step.
    secondApprover: str | None = None


@router.get("/approval")
def approval():
    """Whether layouts need approval, and who approves them."""
    return _approval()


@router.put("/approval")
def set_approval(req: ApprovalSetting):
    """Name a second approver (layouts then need approval), or clear it."""
    named = (req.secondApprover or "").strip()
    if len(named) > 100:
        raise HTTPException(status_code=422, detail="The approver's name is too long.")
    old, unreadable = _setting()
    # Logged before it is changed: a change that cannot be logged is not made.
    if unreadable or old != (named or None):
        _log_setting("(unreadable)" if unreadable else old, named or None)
    _approval_path().write_text(json.dumps({"secondApprover": named or None}),
                                encoding="utf-8")
    return _approval()


@router.get("/approved")
def approved_layouts(purpose: str | None = None):
    """Approved layouts, optionally of one purpose.

    The generation format selector wants generation layouts; a layout an
    earlier install saved for masking is not one.
    """
    out = [l for l in _listed() if l["status"] == "approved"]
    if purpose:
        out = [l for l in out if l.get("purpose", "generation") == purpose]
    return {"layouts": out}


@router.get("/{layout_id}")
def one(layout_id: int):
    """A layout with each field's assigned rule resolved in full.

    field_spec stores only the rule id and a label captured at assignment time.
    Rules are shared and editable, so the detail view resolves them fresh —
    what the field will actually produce, not what it produced when saved.
    """
    layout = ls.get_layout(layout_id)
    if not layout:
        raise HTTPException(status_code=404, detail="Layout not found.")

    # Rules that cannot be read are not deleted rules: refused, rather than
    # every assigned field flagged as missing its rule.
    try:
        by_id = {r["id"]: r for r in ls.get_rules()}
    except Exception as e:
        log.exception("Generation rules could not be read for layout %s", layout_id)
        raise HTTPException(status_code=500, detail=(
            f"The generation rules could not be read, so the layout's fields "
            f"cannot be shown. Try again when they can be read: {plain(e)}"))

    for f in layout.get("fields", []):
        rule = by_id.get(f.get("rule_id"))
        if rule:
            f["rule"] = {
                "id": rule["id"],
                "field_name": rule.get("field_name"),
                "field_label": rule.get("field_label"),
                "gen_method": rule.get("gen_method"),
                "lookup_source": rule.get("lookup_source"),
                "data_type": rule.get("data_type"),
                "length": rule.get("length"),
                "prefix": rule.get("prefix"),
                "postfix": rule.get("postfix"),
                "start_value": rule.get("start_value"),
                "domain": rule.get("domain"),
                "format_pattern": rule.get("format_pattern"),
            }
            # The rule may have been renamed or repointed since assignment.
            f["rule_label"] = rule.get("field_label") or f.get("rule_label")
            f["gen_method"] = rule.get("gen_method") or f.get("gen_method")
        elif f.get("rule_id"):
            f["rule"] = None
            f["rule_missing"] = True

    layout["n_default"] = sum(1 for f in layout.get("fields", [])
                              if f.get("is_default"))
    return layout


class ApproveRequest(BaseModel):
    layout_id: int
    # The second approver's name, typed to confirm: SDM has no sign-in, so
    # typing it is the approver's statement that they reviewed the layout.
    approver: str | None = None


@router.post("/approve")
def approve(req: ApproveRequest):
    """Flip pending_review → approved.

    With a second approver named, the approver's name must be typed (any
    case): the approval is recorded in that name, and confirmed_by records
    the operating-system user who confirmed it. With none named, it is
    recorded in the operating-system user's name."""
    named, unreadable = _setting()
    if unreadable:
        raise HTTPException(status_code=409, detail=(
            "The layout approval setting can't be read. Set the second approver "
            "again in Settings › Layout approval."))
    layout = ls.get_layout(req.layout_id)
    if not layout:
        raise HTTPException(status_code=404, detail="Layout not found.")
    who = _who()
    if named is not None:
        if not same_name(req.approver, named):
            raise HTTPException(status_code=403, detail=(
                "Type the second approver's name to approve this layout."))
    try:
        return ls.approve_layout(req.layout_id, approved_by=named or who,
                                 confirmed_by=who)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approve failed: {plain(e)}")


@router.post("/{layout_id}/withdraw")
def withdraw(layout_id: int):
    """Send an approved layout back to review, without deleting it.

    Only approved layouts are selectable for generation, so this is how you take
    one out of circulation while keeping the definition and its history.
    """
    try:
        return ls.withdraw_approval(layout_id)
    except ValueError as e:
        raise HTTPException(status_code=404 if "No layout" in str(e) else 422,
                            detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.post("/{layout_id}/restore")
def restore(layout_id: int):
    """Bring an archived layout back into service."""
    try:
        return ls.restore_layout(layout_id)
    except ValueError as e:
        raise HTTPException(status_code=404 if "No layout" in str(e) else 422,
                            detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


@router.delete("/{layout_id}")
def remove(layout_id: int):
    """Take a layout out of service.

    An approved layout is archived, not deleted — its approval records that
    somebody reviewed a specific set of fields and rules, and nothing else in
    the system remembers that happened. Anything not approved is deleted, since
    it carries no review and can be re-uploaded from the same sample.

    The response says which happened via `action`, so the caller reports what
    the server actually did rather than what it assumed.
    """
    try:
        return ls.remove_layout(layout_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")


class LayoutEdit(BaseModel):
    fields: list[dict] | None = None
    name: str | None = None
    delimiter: str | None = None
    keep_approval: bool = False


# What the detail view adds to a field for display; not part of what the
# layout writes.
_SHOWN_ONLY = {"rule", "rule_label", "rule_missing", "gen_method", "matched_by"}


def _writes(fields: list[dict]) -> list[dict]:
    return [{k: v for k, v in f.items() if k not in _SHOWN_ONLY} for f in fields]


def _changes_output(current: dict, req: "LayoutEdit") -> bool:
    """Whether an edit changes what the layout writes: its fields (names,
    rules, positions, anything) or its delimiter. Only its name does not."""
    if req.delimiter is not None and req.delimiter != current.get("delimiter"):
        return True
    if req.fields is None:
        return False
    return (json.dumps(_writes(req.fields), sort_keys=True, default=str)
            != json.dumps(_writes(current.get("fields") or []), sort_keys=True,
                          default=str))


@router.put("/{layout_id}")
def update(layout_id: int, req: LayoutEdit):
    """Edit a layout — including an approved one.

    Resets to pending_review unless keep_approval is set, because the approval
    attested to a specific set of fields and rules. Renaming fields is the main
    use: a fixed-width layout arrives as FIELD_1, FIELD_2, which match no
    generated column and therefore render blank.

    While approval is required (a second approver is named), keep_approval
    holds only for an edit that changes nothing the layout writes -- its name;
    any change of fields or delimiter goes back to review.
    """
    keep = req.keep_approval
    if keep and approval_required():
        current = ls.get_layout(layout_id)
        if current is not None and _changes_output(current, req):
            keep = False
    try:
        updated = ls.update_layout(
            layout_id, fields=req.fields, name=req.name,
            delimiter=req.delimiter, keep_approval=keep,
            updated_by=_who(),
        )
    except ls.LayoutArchived as e:
        # Archived, its approval kept: Restore brings it back to be edited.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{plain(e)}")
    if req.fields is not None:
        updated.update(ls.alias_chosen(req.fields))
    return updated
