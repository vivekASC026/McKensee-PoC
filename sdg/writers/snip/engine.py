"""SNIP-style checks of an X12 interchange, driven entirely by rule files.

Nothing here knows an 834 from an 837. What a transaction set requires -- its
segments, its elements, its balancing, its situational rules, its code sets --
is written in `rules/<set>.json` beside this module, and a customer adds to or
changes it in their own directory (`work/snip/`, or `SDG_SNIP_DIR`) without
touching code:

* a customer file named like a shipped one extends it: its rules are added,
  a rule with the same `id` replaces the shipped one, and `"enabled": false`
  switches one off;
* a customer file for a set with no shipped file -- an 837I, a 270 -- is used
  on its own;
* `codesets/<name>.txt` (one code per line, `#` for comments) is a code list;
  a customer list replaces the shipped one of the same name;
* `partners/<name>.json` holds one trading partner's companion-guide rules,
  level 7, applied when that partner is chosen.

The rule kinds are a fixed vocabulary, documented in `rules/README.md`. Level
1 -- X12 syntax and envelopes -- is the standard itself and built in; levels
6 and 7 are whatever the files say.

This is SNIP-style checking, not certification. A certified validator carries
the full licensed implementation guides and code sets; these files carry what
they carry, and the report says which levels were checked and how.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sdg import packs
from sdg.writers.snip.x12 import Interchange, Segment, Transaction, X12Error, parse

HERE = Path(__file__).resolve().parent
SHIPPED = HERE / "rules"
SHIPPED_CODESETS = HERE / "codesets"

LEVELS = {
    1: "X12 syntax and envelopes",
    2: "Implementation guide requirements",
    3: "Balancing",
    4: "Situational rules",
    5: "External code sets",
    6: "Product-type rules",
    7: "Trading-partner rules",
}
MAX_FINDINGS_PER_RULE = 25
# A rule's `pattern` is a regular expression from a rule file; a value longer
# than this is not matched against it, so one long element in an uploaded file
# cannot make a careless pattern backtrack for minutes. X12 elements are far
# shorter.
MAX_PATTERN_VALUE = 512


class RuleFileError(ValueError):
    """A rule file that cannot be read; the message names the file."""


# ---------------------------------------------------------------------------
# Where the rules are
# ---------------------------------------------------------------------------

def customer_dir() -> Path:
    configured = os.getenv("SDG_SNIP_DIR")
    if configured:
        return Path(configured)
    from sdg.config import get_settings

    return get_settings().work_dir / "snip"


def _load(path: Path) -> dict[str, Any]:
    """One rule file, checked enough that a broken one is named, not a 500."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuleFileError(f"The SNIP rule file {path} cannot be read ({exc}). "
                            "Correct or remove it, then check again.") from exc
    if not isinstance(data, dict):
        raise RuleFileError(f"The SNIP rule file {path} is not a JSON object. "
                            "Correct or remove it, then check again.")
    # A rule without a kind can be right -- a customer file switching a shipped
    # rule off by id -- so only the id is required here; a kind still missing
    # once the files are merged is reported when the rule runs.
    rules = data.get("rules", [])
    if not isinstance(rules, list) or any(not isinstance(r, dict) or not r.get("id")
                                          for r in rules):
        raise RuleFileError(f"The SNIP rule file {path} has a rule without an id. "
                            "Give every rule an id, then check again.")
    return data


def rule_files() -> list[dict[str, Any]]:
    """Every rule file in effect, shipped and customer, for the screen to list."""
    packs.require("snip", "SNIP checking")
    out = []
    for origin, folder in (("shipped", SHIPPED), ("customer", customer_dir())):
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                data = _load(path)
                out.append({"origin": origin, "file": str(path), "id": data.get("id", path.stem),
                            "transaction": data.get("transaction"), "version": data.get("version"),
                            "rules": len(data.get("rules", []))})
        partners = folder / "partners"
        if origin == "customer" and partners.exists():
            for path in sorted(partners.glob("*.json")):
                data = _load(path)
                out.append({"origin": "partner", "file": str(path), "id": path.stem,
                            "name": data.get("name", path.stem), "rules": len(data.get("rules", []))})
    return out


def partners() -> list[str]:
    packs.require("snip", "SNIP checking")
    folder = customer_dir() / "partners"
    return sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []


def ruleset_for(transaction: str, version: str, partner: str | None = None) -> dict[str, Any] | None:
    """The shipped rules for this set and version, with the customer's applied."""
    def matching(folder: Path) -> list[dict[str, Any]]:
        if not folder.exists():
            return []
        found = []
        for path in sorted(folder.glob("*.json")):
            data = _load(path)
            if data.get("transaction") == transaction and data.get("version") in (version, None, "*"):
                found.append(data)
        return found

    shipped = matching(SHIPPED)
    custom = matching(customer_dir())
    if not shipped and not custom:
        return None
    base = dict(shipped[0]) if shipped else dict(custom.pop(0))
    rules = {r["id"]: r for r in base.get("rules", [])}
    scopes = list(base.get("scopes", []))
    for extra in custom:
        for rule in extra.get("rules", []):
            rules[rule["id"]] = {**rules.get(rule["id"], {}), **rule}
        scopes = extra.get("scopes", scopes)
    if partner and partner in partners():
        # Only a partner file that is there: the name comes from a request,
        # and one such as "../x" must not reach a path outside the folder.
        path = customer_dir() / "partners" / f"{partner}.json"
        if path.exists():
            for rule in _load(path).get("rules", []):
                rules[rule["id"]] = {"level": 7, **rules.get(rule["id"], {}), **rule}
    base["rules"] = [r for r in rules.values() if r.get("enabled", True)]
    base["scopes"] = scopes
    return base


def codeset(name: str, cache: dict[str, set[str] | None] | None = None) -> set[str] | None:
    """A named code list -- the customer's if they have one -- or None.

    `cache` holds the lists one check has read already, so a list is read once
    per check rather than once per transaction."""
    if cache is not None and name in cache:
        return cache[name]
    found = _read_codeset(name)
    if cache is not None:
        cache[name] = found
    return found


def _read_codeset(name: str) -> set[str] | None:
    for folder in (customer_dir() / "codesets", SHIPPED_CODESETS):
        path = folder / f"{name}.txt"
        if path.exists():
            codes = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                code = line.split("#", 1)[0].strip()
                if code:
                    codes.add(code.split()[0].upper())
            return codes
    return None


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    level: int
    rule: str
    message: str
    segment: int | None = None
    raw: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Report:
    transaction: str = ""
    version: str = ""
    segments: int = 0
    transactions: int = 0
    findings: list[Finding] = field(default_factory=list)
    checked: dict[int, int] = field(default_factory=dict)      # rules run per level
    notes: dict[int, list[str]] = field(default_factory=dict)
    partner: str | None = None
    # Every finding counted, though only the first few of each rule are kept.
    totals: dict[tuple[int, str], int] = field(default_factory=dict)

    def add(self, finding: Finding, per_rule: dict[str, int]) -> None:
        key = (finding.level, finding.severity)
        self.totals[key] = self.totals.get(key, 0) + 1
        per_rule[finding.rule] = per_rule.get(finding.rule, 0) + 1
        if per_rule[finding.rule] <= MAX_FINDINGS_PER_RULE:
            self.findings.append(finding)

    def as_dict(self) -> dict[str, Any]:
        levels = []
        for level, name in LEVELS.items():
            mine = [f for f in self.findings if f.level == level]
            errors = self.totals.get((level, "error"), 0)
            warnings = self.totals.get((level, "warning"), 0)
            ran = self.checked.get(level, 0)
            partial = any("not installed" in n for n in self.notes.get(level, []))
            status = ("not_checked" if not ran else "fail" if errors else
                      "warn" if mine else "partial" if partial else "pass")
            levels.append({"level": level, "name": name, "status": status, "rules": ran,
                           "errors": errors, "warnings": warnings,
                           "notes": self.notes.get(level, []),
                           "findings": [f.as_dict() for f in mine]})
        return {"transaction": self.transaction, "version": self.version,
                "segments": self.segments, "transactions": self.transactions,
                "partner": self.partner,
                "passed": not any(f.severity == "error" for f in self.findings),
                "levels": levels,
                "disclaimer": ("SNIP-style checks from the rule files in effect; not a "
                               "certification. Levels not checked are said so.")}


# ---------------------------------------------------------------------------
# Level 1: the standard itself
# ---------------------------------------------------------------------------

_ISA_WIDTHS = [2, 10, 2, 10, 2, 15, 2, 15, 6, 4, 1, 5, 9, 1, 1, 1]
_SEGMENT_ID = re.compile(r"^[A-Z0-9]{2,3}$")


def _envelopes(ix: Interchange, report: Report, seen: dict[str, int]) -> None:
    report.checked[1] = report.checked.get(1, 0) + 6

    def err(rule, message, seg=None):
        report.add(Finding(1, rule, message, seg.index if seg else None,
                           seg.raw[:120] if seg else None), seen)

    isa = ix.isa
    if isa is not None:
        if len(isa.elements) != 16:
            err("isa-elements", f"ISA has {len(isa.elements)} elements; it must have 16.", isa)
        else:
            for position, (value, width) in enumerate(zip(isa.elements, _ISA_WIDTHS), start=1):
                if len(value) != width:
                    err("isa-width", f"ISA{position:02d} is {len(value)} characters; it must be {width}.", isa)
    if ix.iea is None:
        err("iea-missing", "The interchange has no IEA.")
    else:
        if isa is not None and ix.iea.get(2) != isa.get(13):
            err("iea-control", f"IEA02 {ix.iea.get(2)} does not match ISA13 {isa.get(13)}.", ix.iea)
        if ix.iea.get(1) != str(len(ix.groups)):
            err("iea-count", f"IEA01 says {ix.iea.get(1)} groups; there are {len(ix.groups)}.", ix.iea)
    for group in ix.groups:
        if group.ge is None:
            err("ge-missing", "A functional group has no GE.", group.gs)
            continue
        if group.ge.get(2) != group.gs.get(6):
            err("ge-control", f"GE02 {group.ge.get(2)} does not match GS06 {group.gs.get(6)}.", group.ge)
        if group.ge.get(1) != str(len(group.transactions)):
            err("ge-count", f"GE01 says {group.ge.get(1)} transactions; there are {len(group.transactions)}.", group.ge)
        for tx in group.transactions:
            if tx.se is None:
                err("se-missing", "A transaction has no SE.", tx.st)
                continue
            if tx.se.get(2) != tx.st.get(2):
                err("se-control", f"SE02 {tx.se.get(2)} does not match ST02 {tx.st.get(2)}.", tx.se)
            if tx.se.get(1) != str(len(tx.segments)):
                err("se-count", f"SE01 says {tx.se.get(1)} segments; ST to SE holds {len(tx.segments)}.", tx.se)
            if tx.st.get(3) and group.gs.get(8) and tx.st.get(3) != group.gs.get(8):
                err("st-version", f"ST03 {tx.st.get(3)} differs from GS08 {group.gs.get(8)}.", tx.st)
    for seg in ix.stray:
        if seg.id not in ("ISA", "IEA"):
            err("outside-envelope", f"{seg.id} sits outside a transaction.", seg)
    for seg in ix.segments:
        if not _SEGMENT_ID.match(seg.id):
            err("segment-id", f"'{seg.id}' is not a valid segment identifier.", seg)
        if seg.trailing_separator and seg.id != "ISA":
            err("trailing-separator", f"{seg.id} ends with an empty element; X12 does not allow "
                "trailing separators.", seg)
        if any(ord(ch) < 32 for ch in seg.raw):
            err("control-character", f"{seg.id} contains a control character.", seg)


# ---------------------------------------------------------------------------
# Levels 2-7: rule kinds
# ---------------------------------------------------------------------------

def _scopes_of(tx: Transaction, scopes: list[dict[str, Any]]) -> list[dict[str, int]]:
    """For each segment, the occurrence number of every scope it falls in.

    A scope opens on a segment (optionally with a qualifier) and lasts until
    another opener of the same or an outer scope, or the end of the set.
    Scopes are listed outermost first.
    """
    names = [s["name"] for s in scopes]
    counters = {n: 0 for n in names}
    current: dict[str, int] = {}
    out = []
    for seg in tx.segments:
        for depth, scope in enumerate(scopes):
            if seg.id == scope["opens"] and _qualifies(seg, scope.get("when")):
                counters[scope["name"]] += 1
                current[scope["name"]] = counters[scope["name"]]
                for inner in names[depth + 1:]:
                    current.pop(inner, None)
                break
        out.append(dict(current))
    return out


def _qualifies(seg: Segment, when: dict[str, Any] | None) -> bool:
    if not when:
        return True
    value = _value(seg, when.get("position", 1), when.get("component"), None)
    if "in" in when:
        return value in when["in"]
    if "equals" in when:
        return value == when["equals"]
    if "not_in" in when:
        return value not in when["not_in"]
    return True


def _value(seg: Segment, position: int, component: int | None, separator: str | None) -> str:
    raw = seg.get(position)
    if component:
        parts = raw.split(separator or ":")
        return parts[component - 1] if component <= len(parts) else ""
    return raw


def _date_ok(text: str) -> bool:
    for fmt in ("%Y%m%d", "%y%m%d"):
        if len(text) == len(datetime.now().strftime(fmt)):
            try:
                datetime.strptime(text, fmt)
                return True
            except ValueError:
                return False
    return False


def _type_ok(kind: str, text: str) -> str | None:
    if kind == "N0" and not re.fullmatch(r"-?\d+", text):
        return "is not a whole number"
    if kind == "N2" and not re.fullmatch(r"-?\d+", text):
        return "is not a number with implied decimals"
    # Written so no digit can be matched two ways: "\d+\.?\d*" backtracks
    # quadratically on a long run of digits that ends badly.
    if kind == "R" and not re.fullmatch(r"-?(\d+(\.\d*)?|\.\d+)", text):
        return "is not a decimal number"
    if kind == "DT" and not _date_ok(text):
        return "is not a valid date (CCYYMMDD)"
    if kind == "TM" and not re.fullmatch(r"([01]\d|2[0-3])[0-5]\d([0-5]\d(\d{1,2})?)?", text):
        return "is not a valid time (HHMM)"
    return None


def _luhn_npi(npi: str) -> bool:
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = "80840" + npi[:9]
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10 == int(npi[9])


def _amount(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _pattern(text: str) -> re.Pattern[str] | None:
    """A rule's pattern, compiled once; None where it is not a valid one."""
    try:
        return _compiled(text)
    except re.error:
        return None


@lru_cache(maxsize=256)
def _compiled(text: str) -> re.Pattern[str]:
    return re.compile(text)


def _run_rule(rule: dict[str, Any], tx: Transaction, scope_ids: list[dict[str, int]],
              sep: str, report: Report, seen: dict[str, int],
              codesets: dict[str, set[str] | None] | None = None) -> None:
    level = int(rule.get("level", 2))
    kind = rule.get("kind")
    severity = rule.get("severity", "error")
    rid = rule["id"]

    def flag(message: str, seg: Segment | None = None) -> None:
        # A rule file may word its own finding; otherwise the engine's wording.
        report.add(Finding(level, rid, rule.get("message") or message,
                           seg.index if seg else None, seg.raw[:120] if seg else None,
                           severity), seen)

    def selected(segment_id: str, when: dict | None = None):
        return [(i, s) for i, s in enumerate(tx.segments)
                if s.id == segment_id and _qualifies(s, when)]

    scope = rule.get("scope")

    if kind == "required_segment":
        hits = selected(rule["segment"], rule.get("when"))
        label = rule.get("label") or rule["segment"]
        if scope:
            # The first segment of each occurrence, found in one pass.
            openers: dict[int, Segment] = {}
            for i, ids in enumerate(scope_ids):
                if scope in ids:
                    openers.setdefault(ids[scope], tx.segments[i])
            have: dict[int, int] = {}
            for i, _ in hits:
                if scope in scope_ids[i]:
                    have[scope_ids[i][scope]] = have.get(scope_ids[i][scope], 0) + 1
            for occurrence in sorted(openers):
                n = have.get(occurrence, 0)
                if n < rule.get("min", 1):
                    opener = openers[occurrence]
                    flag(f"{label} is required in every {scope} loop; this one has none.", opener)
                elif rule.get("max") and n > rule["max"]:
                    flag(f"{label} appears {n} times in one {scope}; at most {rule['max']}.")
        else:
            if len(hits) < rule.get("min", 1):
                flag(f"{label} is required in the transaction and is missing.", tx.st)
            elif rule.get("max") and len(hits) > rule["max"]:
                flag(f"{label} appears {len(hits)} times; at most {rule['max']}.")

    elif kind == "element":
        name = rule.get("label") or f"{rule['segment']}{rule['position']:02d}" + (
            f"-{rule['component']}" if rule.get("component") else "")
        allowed = set(rule["codes"]) if rule.get("codes") else None
        listed = codeset(rule["code_set"], codesets) if rule.get("code_set") else None
        if rule.get("code_set") and listed is None:
            report.notes.setdefault(level, []).append(
                f"{name}: code set '{rule['code_set']}' is not installed; membership not checked.")
        pattern = _pattern(rule["pattern"]) if rule.get("pattern") else None
        if rule.get("pattern") and pattern is None:
            note = (f"{name}: the rule's pattern is not a valid regular expression; "
                    "its form was not checked. Correct the pattern in the rule file.")
            if note not in report.notes.get(level, []):
                report.notes.setdefault(level, []).append(note)
        for _, seg in selected(rule["segment"], rule.get("when")):
            value = _value(seg, rule["position"], rule.get("component"), sep)
            if not value:
                if rule.get("required"):
                    flag(f"{name} is required and is empty.", seg)
                continue
            if rule.get("min") and len(value) < rule["min"]:
                flag(f"{name} '{value}' is shorter than {rule['min']}.", seg)
            if rule.get("max") and len(value) > rule["max"]:
                flag(f"{name} '{value}' is longer than {rule['max']}.", seg)
            problem = _type_ok(rule.get("type", "AN"), value)
            if problem:
                flag(f"{name} '{value}' {problem}.", seg)
            if allowed is not None and value not in allowed:
                flag(f"{name} '{value}' is not an allowed code ({', '.join(sorted(allowed)[:8])}"
                     f"{'...' if len(allowed) > 8 else ''}).", seg)
            if listed is not None and value.upper().replace(".", "") not in listed:
                flag(f"{name} '{value}' is not in code set '{rule['code_set']}'.", seg)
            if pattern is not None and len(value) > MAX_PATTERN_VALUE:
                flag(f"{name} is {len(value)} characters, too long to check against "
                     f"{rule.get('pattern_label') or 'its expected form'}.", seg)
            elif pattern is not None and not pattern.fullmatch(value):
                flag(f"{name} '{value}' does not have the expected form "
                     f"{rule.get('pattern_label') or rule['pattern']}.", seg)
            if rule.get("check") == "npi" and not _luhn_npi(value):
                flag(f"{name} '{value}' fails the NPI check digit.", seg)

    elif kind == "both_or_neither":
        for _, seg in selected(rule["segment"], rule.get("when")):
            present = [bool(seg.get(p)) for p in rule["positions"]]
            if any(present) and not all(present):
                flag(f"{rule['segment']} must carry " + " and ".join(
                    f"{rule['segment']}{p:02d}" for p in rule["positions"]) + " together.", seg)

    elif kind == "followed_by":
        for i, seg in selected(rule["segment"], rule.get("when")):
            nxt = tx.segments[i + 1] if i + 1 < len(tx.segments) else None
            if nxt is None or nxt.id != rule["then"]:
                flag(f"{rule['segment']} must be followed by {rule['then']}.", seg)

    elif kind == "conditional":
        # When `if` holds on a segment, `then` must hold: another element of
        # it, or a segment within the same scope.
        cond, then = rule["if"], rule["then"]
        # Where a `then` segment is, by occurrence of the scope, found in one
        # pass rather than once per segment the rule applies to.
        holding: set[Any] | None = None
        if "segment" in then and then["segment"] != cond["segment"]:
            holding = {scope_ids[j].get(scope) if scope else None
                       for j, s in enumerate(tx.segments)
                       if s.id == then["segment"] and _qualifies(s, then.get("when"))}
        for i, seg in selected(cond["segment"], cond.get("when")):
            if not _qualifies(seg, cond):
                continue
            if holding is not None:
                within = scope_ids[i].get(scope) if scope else None
                if within not in holding:
                    flag(rule.get("text", f"{then['segment']} is required here."), seg)
            else:
                value = _value(seg, then["position"], then.get("component"), sep)
                ok = (value in then["in"]) if "in" in then else \
                     (value == then["equals"]) if "equals" in then else bool(value)
                if not ok:
                    flag(rule.get("text", f"{cond['segment']}{then['position']:02d} '{value}' "
                                          "does not satisfy the rule."), seg)

    elif kind == "date_by_qualifier":
        for _, seg in selected(rule["segment"], rule.get("when")):
            fmt = seg.get(rule["format_position"])
            value = seg.get(rule["value_position"])
            if fmt == "D8" and not (len(value) == 8 and _date_ok(value)):
                flag(f"{seg.id} says D8 but '{value}' is not a CCYYMMDD date.", seg)
            elif fmt == "RD8":
                parts = value.split("-")
                if len(parts) != 2 or not all(len(p) == 8 and _date_ok(p) for p in parts) \
                        or parts[0] > parts[1]:
                    flag(f"{seg.id} says RD8 but '{value}' is not a CCYYMMDD-CCYYMMDD range.", seg)

    elif kind == "sum":
        # total == sum(parts) [- sum(less)] within each occurrence of `scope`
        # (or across the transaction).
        tolerance = float(rule.get("tolerance", 0.005))
        groups: dict[Any, dict[str, Any]] = {}
        for i, seg in enumerate(tx.segments):
            key = scope_ids[i].get(scope) if scope else 0
            if scope and key is None:
                continue
            g = groups.setdefault(key, {"total": None, "seg": None, "parts": 0.0, "less": 0.0})
            t = rule["total"]
            if seg.id == t["segment"] and _qualifies(seg, t.get("when")) and g["total"] is None:
                g["total"], g["seg"] = _amount(_value(seg, t["position"], t.get("component"), sep)), seg
            for name in ("parts", "less"):
                spec = rule.get(name)
                if spec and seg.id == spec["segment"] and _qualifies(seg, spec.get("when")):
                    amount = _amount(_value(seg, spec["position"], spec.get("component"), sep))
                    g[name] += amount or 0.0
        for g in groups.values():
            if g["total"] is None:
                continue
            expected = g["parts"] - g["less"]
            if abs(g["total"] - expected) > tolerance:
                flag(f"{rule.get('label', 'Total')} {g['total']:.2f} does not equal "
                     f"{rule.get('parts_label', 'its parts')} {expected:.2f}.", g["seg"])

    else:
        report.notes.setdefault(level, []).append(f"Rule {rid}: unknown kind '{kind}', not run.")
        return

    report.checked[level] = report.checked.get(level, 0) + 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate(text: str, partner: str | None = None) -> dict[str, Any]:
    packs.require("snip", "SNIP checking")
    report = Report(partner=partner)
    seen: dict[str, int] = {}
    try:
        ix = parse(text)
    except X12Error as e:
        report.add(Finding(1, "not-x12", str(e)), seen)
        report.checked[1] = 1
        return report.as_dict()

    report.segments = len(ix.segments)
    _envelopes(ix, report, seen)
    # The rule files and code lists are read once per check, not once per
    # transaction: a large upload holds thousands of transactions.
    rulesets: dict[tuple[str, str], dict[str, Any] | None] = {}
    codesets: dict[str, set[str] | None] = {}

    for group in ix.groups:
        for tx in group.transactions:
            report.transactions += 1
            transaction, version = tx.st.get(1), tx.st.get(3) or group.gs.get(8)
            report.transaction, report.version = transaction, version
            if (transaction, version) not in rulesets:
                rulesets[(transaction, version)] = ruleset_for(transaction, version, partner)
            rules = rulesets[(transaction, version)]
            if rules is None:
                for level in (2, 3, 4, 5):
                    report.notes.setdefault(level, []).append(
                        f"No rule file for {transaction} {version}; add one to {customer_dir()}.")
                continue
            if rules.get("functional_code") and group.gs.get(1) != rules["functional_code"]:
                report.add(Finding(1, "gs-functional-code",
                                   f"GS01 {group.gs.get(1)} is not {rules['functional_code']}, "
                                   f"the code for {transaction}.", group.gs.index, group.gs.raw), seen)
            scope_ids = _scopes_of(tx, rules.get("scopes", []))
            for rule in rules["rules"]:
                _run_rule(rule, tx, scope_ids, ix.component_separator, report, seen, codesets)

    for level in (6, 7):
        if not report.checked.get(level):
            report.notes.setdefault(level, []).append(
                "No rules at this level in the files in effect." if level == 6 else
                "No trading partner chosen." if not partner else
                f"Partner '{partner}' has no rules.")
    for rid, n in seen.items():
        if n > MAX_FINDINGS_PER_RULE:
            level = next((f.level for f in report.findings if f.rule == rid), 1)
            report.notes.setdefault(level, []).append(
                f"{rid}: {n} findings; the first {MAX_FINDINGS_PER_RULE} are shown.")
    return report.as_dict()
