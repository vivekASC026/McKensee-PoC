"""FHIR R4 JSON read and written for files modeled on a sample: the syntax, never a kind of file.

The sample is a Bundle (or a single resource, read as a collection Bundle of
one). The file written is the sample's Bundle with its entries repeated per
record: every resource and value the sample has, except the places a
generation config names. What repeats (a Patient and what follows it; a
Claim; a Claim's items), what follows from what and what a place holds are
in the profile (`profiles/fhir*.json`).

The config is the masking tool's FHIR config (edi-mask `FHIRR4Config.json`),
with `genResources` / `genPaths` for `maskResources` / `maskPaths`: per
`resourceType`, a `path` (`name[*].family`; `[*]` every occurrence) and an
optional `where` testing sibling fields (`telecom[*].value` where `system` is
`phone`). A masking config, comments and all, can be read as the master.

Where this module stops is FHIR's own grammar, the same for every resource:
JSON types (a number stays a number), a Coding's `display` dropped when its
`code` is generated (it would describe the sample's code), a HumanName's or
Address's `text` said again from its parts (the profile's `texts`), and how
one resource refers to another (`Patient/123`, a `fullUrl`), which `rename`
moves together.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Iterator

from sdg.writers.samples import engine
from sdg.writers.samples import profile as profiles
from sdg.writers.samples.base import SampleError, without_comments

FORMAT = "fhir"
LABEL = "FHIR R4 JSON"
MEDIA = "application/fhir+json"
EXTENSION = ".json"
VALIDATION = "fhir"

ROOT_KEYS = ("genResources", "maskResources")
PATH_KEYS = ("genPaths", "maskPaths")

_PLACEHOLDER = "@@SDM-ENTRIES@@"
# Deeper than any FHIR resource nests; a document deeper than this is refused
# before anything walks it.
MAX_DEPTH = 64
# The parts of a Bundle's `meta` that say what it conforms to, kept; the rest
# (versionId, lastUpdated, source) describe the sample's own copy.
_META_KEPT = ("profile", "tag", "security")
# Conditional headers of an entry's request: they name the sample's own
# identifiers and versions, not the resource written.
_REQUEST_DROPPED = ("ifNoneExist", "ifMatch", "ifNoneMatch", "ifModifiedSince")


def _too_deep(doc: Any, limit: int = MAX_DEPTH) -> bool:
    """Whether a parsed document nests deeper than `limit`, found without recursion."""
    stack = [(doc, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        stack.extend((child, depth + 1) for child in children
                     if isinstance(child, (dict, list)))
    return False


def _fresh(value: Any, seed: str, what: str) -> str:
    """A value of the request's own for one the sample's Bundle had, written
    as the sample's was (a urn:uuid: stays one)."""
    made = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sdm-bundle-{what}:{seed}"))
    return f"urn:uuid:{made}" if str(value).startswith("urn:uuid:") else made


def _bundle_shell(bundle: dict[str, Any], seed: str) -> dict[str, Any]:
    """The sample's Bundle without its entries and without what identifies the
    sample's own copy: its id and identifier are the request's, its meta keeps
    only what it conforms to, and its links, signature and total go (a total
    is written after the entries, counted)."""
    shell = {k: copy.deepcopy(v) for k, v in bundle.items()
             if k not in ("entry", "total", "link", "signature")}
    if "id" in shell:
        shell["id"] = _fresh(shell["id"], seed, "id")
    identifier = shell.get("identifier")
    if isinstance(identifier, dict) and "value" in identifier:
        identifier["value"] = _fresh(identifier["value"], seed, "identifier")
    if isinstance(shell.get("meta"), dict):
        kept = {k: v for k, v in shell["meta"].items() if k in _META_KEPT}
        if kept:
            shell["meta"] = kept
        else:
            del shell["meta"]
    return shell


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class At:
    """'Patient name[*].family': a resource type ('*' any) and a path in it."""
    type: str
    path: str


@lru_cache(maxsize=None)
def at(text: str) -> At:
    parts = text.split(None, 1)
    if len(parts) != 2:
        raise SampleError(f"Profile place {text!r} is not written as 'Patient name[*].family'.")
    return At(parts[0], parts[1])


@lru_cache(maxsize=None)
def _steps(path: str) -> tuple[tuple[str, bool], ...]:
    """'name[*].given[*]' -> (('name', True), ('given', True))."""
    out = []
    for piece in path.split("."):
        m = re.fullmatch(r"([A-Za-z_][\w]*)(\[\*\])?", piece)
        if m is None:
            raise SampleError(f"FHIR path {path!r}: {piece!r} is not a field (only [*] is allowed).")
        out.append((m.group(1), bool(m.group(2))))
    return tuple(out)


def _find(obj: Any, path: str, where: dict[str, Any] | None = None) -> list[tuple[Any, Any, dict]]:
    """Every place the path reaches: (container, key, the object that owns the field)."""
    found: list[tuple[Any, Any, dict]] = []

    def walk(node: Any, steps):
        (name, many), rest = steps[0], steps[1:]
        if not isinstance(node, dict) or name not in node:
            return
        value = node[name]
        if many:
            if not isinstance(value, list):
                return
            for i, element in enumerate(value):
                if rest:
                    walk(element, rest)
                else:
                    found.append((value, i, node))
        elif rest:
            walk(value, rest)
        else:
            found.append((node, name, node))

    walk(obj, _steps(path))
    if where:
        found = [f for f in found if all(f[2].get(k) == v for k, v in where.items())]
    return found


def _leaves(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any, Any, dict]]:
    """Every scalar under obj: (normalized path, container, key, owning object)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{prefix}.{key}" if prefix else key
            if isinstance(value, list):
                for i, element in enumerate(value):
                    if isinstance(element, (dict, list)):
                        yield from _leaves(element, here + "[*]")
                    else:
                        yield here + "[*]", value, i, obj
            elif isinstance(value, dict):
                yield from _leaves(value, here)
            else:
                yield here, obj, key, obj


def _typed(value: str, like: Any) -> Any:
    """A generated value in the JSON type the sample has at that place."""
    if isinstance(like, bool):
        return like
    if isinstance(like, int):
        try:
            return int(float(value))
        except ValueError:
            return like
    if isinstance(like, float):
        try:
            return float(value)
        except ValueError:
            return like
    return value


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    type: str
    path: str
    where: tuple[tuple[str, Any], ...]
    name: str


def read_config(body: dict | str) -> list[Entry]:
    """A generation config, or a masking config read as one."""
    if isinstance(body, str):
        try:
            body = json.loads(without_comments(body))
        except ValueError as exc:
            raise SampleError(f"The config is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise SampleError("The config is not a JSON object.")
    root = next((k for k in ROOT_KEYS if k in body), None)
    if root is None:
        raise SampleError("The config has no genResources (or maskResources) list.")
    entries = []
    for index, block in enumerate(body.get(root) or []):
        if not isinstance(block, dict) or not block.get("resourceType"):
            raise SampleError(f"{root}[{index}] has no resourceType.")
        for rule in next((block[k] for k in PATH_KEYS if k in block), None) or []:
            name = str((rule.get("algorithm_reference") or {}).get("name") or "").strip()
            if not name or not rule.get("path"):
                continue        # a masking "regenerate" rebuilds; nothing to generate
            _steps(rule["path"])
            where = tuple(sorted((rule.get("where") or {}).items()))
            entries.append(Entry(block["resourceType"], rule["path"], where, name))
    return entries


def write_config(rows: Iterable[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    """Rows chosen on the screen as a genResources config."""
    blocks: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("algorithm") or "").strip()
        if not name:
            continue
        block = blocks.setdefault(row["resourceType"],
                                  {"resourceType": row["resourceType"], "genPaths": []})
        one: dict[str, Any] = {"path": row["path"]}
        if row.get("where"):
            one["where"] = row["where"]
        one["algorithm_reference"] = {"name": name}
        block["genPaths"].append(one)
    return {**{k: v for k, v in meta.items() if v is not None},
            "format": "FHIR R4", "genResources": list(blocks.values())}


def positions(config: dict[str, Any]) -> int:
    return sum(len(b.get("genPaths") or []) for b in config.get("genResources") or [])


def used(entries: list[Entry]) -> set[str]:
    return {e.name for e in entries}


# ---------------------------------------------------------------------------
# The sample
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    text: str
    bundle: dict[str, Any]       # the sample's Bundle, or a collection made around one resource
    entries: list[dict[str, Any]]
    single: bool
    kind: str
    profile: dict[str, Any]
    version: str = "R4"


def detect(text: str) -> bool:
    head = text.lstrip("﻿ \r\n\t")
    return head.startswith("{") and '"resourceType"' in head[:4000]


def _types(sample_entries: list[dict]) -> list[str]:
    return [str((e.get("resource") or {}).get("resourceType") or "") for e in sample_entries]


def _selector(text: str):
    """'Claim|ExplanationOfBenefit': a resource of either type."""
    wanted = {t.strip() for t in text.split("|")}
    return lambda resource_type: resource_type in wanted


def _profile_for(types: list[str]) -> dict[str, Any] | None:
    """The profile whose `match` the sample satisfies most fully. A `fallback`
    profile (one resource on its own: a Coverage, a Claim) is taken only when
    no other fits as well, so a Patient with its Coverage stays members."""
    best, score = None, -1.0
    for kind in profiles.kinds(FORMAT):
        prof = profiles.for_kind(FORMAT, kind)
        need = prof.get("match") or []
        mine = len(need) - (0.5 if prof.get("fallback") else 0)
        if all(any(_selector(sel)(t) for t in types) for sel in need) and mine > score:
            best, score = prof, mine
    return best


def subject(kind: str) -> str:
    return (profiles.for_kind(FORMAT, kind) or {}).get("subject", "members")


def facts(sample: Sample) -> tuple[dict[str, int], str]:
    types = _types(sample.entries)
    counts = {name: sum(1 for t in types if _selector(sel)(t))
              for name, sel in (sample.profile.get("counts") or {}).items()}
    counts["resources"] = len(types)
    return counts, str(sample.profile.get("summary") or "").format(**counts)


def read_sample(text: str) -> Sample:
    try:
        doc = json.loads(text.lstrip("﻿"))
    except ValueError as exc:
        raise SampleError(f"The sample is not JSON: {exc}") from None
    except RecursionError:
        doc = deep = True
    else:
        deep = _too_deep(doc)
    if deep:
        raise SampleError(f"The sample nests deeper than {MAX_DEPTH} levels, which no FHIR "
                          "resource does. Choose a FHIR resource or Bundle.")
    if not isinstance(doc, dict) or not doc.get("resourceType"):
        raise SampleError("The sample is not a FHIR resource: it has no resourceType.")
    single = doc["resourceType"] != "Bundle"
    bundle = ({"resourceType": "Bundle", "type": "collection", "entry": [{"resource": doc}]}
              if single else doc)
    entries = [e for e in bundle.get("entry") or [] if isinstance(e, dict) and e.get("resource")]
    if not entries:
        raise SampleError("The sample Bundle has no entries to model on.")
    prof = _profile_for(_types(entries))
    if prof is None:
        kinds = "; ".join(f"{k} ({' + '.join(profiles.for_kind(FORMAT, k).get('match') or [])})"
                          for k in profiles.kinds(FORMAT))
        raise SampleError(f"The sample's resources ({', '.join(sorted(set(_types(entries))))}) "
                          f"fit no FHIR profile; a file can be modeled on: {kinds}.")
    return Sample(text=text, bundle=bundle, entries=entries, single=single,
                  kind=prof["kind"], profile=prof)


# ---------------------------------------------------------------------------
# Proposing an algorithm for every place of the sample
# ---------------------------------------------------------------------------

def _qualifier(prof: dict[str, Any], path: str, owner: dict) -> tuple[tuple[str, Any], ...]:
    """The sibling fields that say what a value is (telecom's system)."""
    leaf = path.rsplit(".", 1)[-1].replace("[*]", "")
    keys = (prof.get("qualified") or {}).get(leaf) or []
    return tuple((k, owner[k]) for k in keys if k in owner and not isinstance(owner[k], (dict, list)))


def _places(prof: dict[str, Any]):
    out = []
    for p in (prof.get("places") or {}).get("list") or []:
        a = at(p["at"])
        out.append((a.type, a.path, tuple(sorted((p.get("where") or {}).items())), p["is"]))
    return out


def _place(prof, places, resource_type: str, path: str, where) -> tuple[str | None, bool]:
    member = set((prof.get("member") or {}).get("types") or [])
    if member and resource_type not in member:
        return None, True
    for t, p, w, element in places:
        if t in (resource_type, "*") and p == path and set(w) <= set(where):
            return element, element is None
    return None, False


def _label(resource_type: str, path: str, where) -> str:
    text = f"{resource_type} {path}"
    if where:
        text += " (" + ", ".join(f"{k}={v}" for k, v in where) + ")"
    return text


def _derived(prof: dict[str, Any]) -> set[tuple[str, str]]:
    texts = [f"{t['at']}.text" for t in (prof.get("texts") or {}).get("list") or []]
    return {(at(t).type, at(t).path) for t in [*engine.derived_targets(prof), *texts]}


def _said(obj: dict[str, Any], template: str) -> str:
    """A template's {field}s from an object (a list's items one after another),
    the pieces it does not have left out with their commas."""
    def field(m: re.Match) -> str:
        value = obj.get(m.group(1))
        if isinstance(value, list):
            return " ".join(str(v) for v in value if v not in (None, ""))
        return "" if value is None else str(value)

    text = re.sub(r"\{(\w+)\}", field, template)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*,(\s*,)*\s*", ", ", text)
    return text.strip(" ,")


def propose(sample: Sample, entries: list[Entry], algorithms: Iterable[str]) -> list[dict[str, Any]]:
    """Every value in the sample's resources, and what the config says for it."""
    prof = sample.profile
    known = {a.upper() for a in algorithms}
    derived = _derived(prof)
    structural = set((prof.get("structural") or {}).get("fields") or [])
    kinds: dict[tuple, dict[str, Any]] = {}
    for entry in sample.entries:
        resource = entry["resource"]
        resource_type = resource["resourceType"]
        for path, container, key, owner in _leaves(resource):
            last = path.rsplit(".", 1)[-1].replace("[*]", "")
            value = container[key]
            if (last in structural or path.startswith("text.") or isinstance(value, bool)
                    or value in (None, "") or (resource_type, path) in derived
                    or ("*", path) in derived):
                continue
            where = _qualifier(prof, path, owner)
            slot = kinds.setdefault((resource_type, path, where),
                                    {"count": 0, "example": value, "resources": set()})
            slot["resources"].add(id(resource))
    for slot in kinds.values():
        slot["count"] = len(slot.pop("resources"))

    places = _places(prof)
    rows = []
    for (resource_type, path, where), slot in kinds.items():
        config_name = next((e.name for e in entries if e.type == resource_type
                            and e.path == path and set(e.where) <= set(where)), None)
        place, blocked = _place(prof, places, resource_type, path, where)
        algorithm, reason = engine.decide(config_name, place, blocked, known)
        rows.append({
            "resourceType": resource_type, "path": path, "where": dict(where) or None,
            "label": _label(resource_type, path, where), "example": str(slot["example"]),
            "count": slot["count"], "config_name": config_name, "algorithm": algorithm,
            "reason": reason, "suggested": place if place in known else None,
            "kept": reason.startswith(engine.KEPT),
        })
    return rows


# ---------------------------------------------------------------------------
# Writing: FHIR's reader for the engine
# ---------------------------------------------------------------------------

@dataclass
class Tpl:
    type: str
    base: str                      # "" for a resource; "item[*]" for an element repeated as a unit
    obj: dict[str, Any]            # the resource (or element) as the sample has it
    wrapper: dict[str, Any] | None  # the Bundle entry around a resource: fullUrl, request...
    fills: list[tuple[str, tuple, str]]   # (path below base, where, element)
    codes: list[dict[str, Any]]
    holes: list[str] = field(default_factory=list)   # arrays a child unit fills


@dataclass
class Item:
    type: str
    base: str
    obj: dict[str, Any]
    wrapper: dict[str, Any] | None
    tpl: Tpl
    holes: list[str]


def _fills(entries: list[Entry], resource_type: str, base: str, known: set[str]):
    out = []
    for e in entries:
        if e.type != resource_type or e.name.upper() not in known:
            continue
        if base:
            if not e.path.startswith(base + "."):
                continue
            path = e.path[len(base) + 1:]
        else:
            path = e.path
        out.append((path, e.where, e.name.upper()))
    return out


class Reader:
    """FHIR for the engine: one per file written (it knows which entry comes first)."""

    def __init__(self, sample: Sample, seed: str = "fhir"):
        self.first = True
        # The request's seed: the Bundle's id and identifier are made from it.
        self.seed = seed
        self.count: int | None = None
        self.texts = [(at(t["at"]), t["text"])
                      for t in (sample.profile.get("texts") or {}).get("list") or []]

    # -- the sample's shape --------------------------------------------------

    def shape(self, sample: Sample, entries: list[Entry], known: set[str]) -> engine.Shape:
        prof = sample.profile
        codes = list((prof.get("codes") or {}).get("list") or [])
        tpls = []
        for e in sample.entries:
            resource = e["resource"]
            wrapper = {k: v for k, v in e.items() if k != "resource"}
            tpls.append(Tpl(resource["resourceType"], "", resource, wrapper,
                            _fills(entries, resource["resourceType"], "", known), codes))
        units = prof.get("units") or []
        if not units:
            raise SampleError(f"Profile {prof['name']} names no unit to repeat.")
        head, occurrences = self._level(tpls, units[0], entries, known, codes)
        if not occurrences:
            raise SampleError(units[0].get("missing") or
                              f"The sample has no {units[0]['name']} to model on.")
        return engine.Shape(head, occurrences, units[0])

    def _level(self, tpls: list[Tpl], spec: dict[str, Any], entries, known, codes):
        starts = _selector(spec["starts"])
        head, occurrences = engine.cut(tpls, spec, lambda t: starts(t.type))
        child = (spec.get("units") or [None])[0]
        out = []
        for occ in occurrences:
            if child is None:
                out.append(engine.Unit(spec, occ))
                continue
            if child.get("in"):
                # The child repeats inside the unit's first resource (a Claim's items).
                host = occ[0]
                kids = self._inside(host, child, entries, known, codes)
                occ = [Tpl(host.type, host.base, _without(host.obj, child["in"]), host.wrapper,
                           host.fills, codes, [*host.holes, child["in"]]), *occ[1:]]
                prefix = occ
            else:
                prefix, kids = self._level(occ, child, entries, known, codes)
            if spec.get("requires") and not kids:
                continue
            out.append(engine.Unit(spec, prefix, kids))
        return head, out

    @staticmethod
    def _inside(host: Tpl, spec: dict[str, Any], entries, known, codes) -> list[engine.Unit]:
        base = spec["in"]
        units = []
        for container, key, _owner in _find(host.obj, base):
            element = container[key]
            if isinstance(element, dict):
                tpl = Tpl(host.type, base, element, None,
                          _fills(entries, host.type, base, known), codes)
                units.append(engine.Unit(spec, [tpl]))
        return units

    # -- filling -------------------------------------------------------------

    def fill(self, tpl: Tpl, record: dict[str, Any], row: int, values) -> Item:
        obj = copy.deepcopy(tpl.obj)
        for vary in values.vary:
            a = at(vary.at)
            if a.type in (tpl.type, "*"):
                rel = a.path if not tpl.base else (
                    a.path[len(tpl.base) + 1:] if a.path.startswith(tpl.base + ".") else None)
                if rel:
                    _vary(obj, rel, vary, values, row)
        for path, where, element in tpl.fills:
            for k, (container, key, owner) in enumerate(_find(obj, path, dict(where))):
                # One record holds one value per element: the first place takes it,
                # any other occurrence (a maiden name, a second given name) the rule's.
                made = values(element, record, row) if k == 0 else values(element, {}, row * 100 + k)
                if not made:
                    continue
                current = container[key]
                full = f"{tpl.base}.{path}" if tpl.base else path
                coded = self._code(tpl, full, element, made)
                new = _typed(coded if coded != made else engine.fit(made, str(current)), current)
                if new != current and key == "code" and isinstance(owner, dict):
                    owner.pop("display", None)
                container[key] = new
        # A summary text (HumanName.text, Address.text) says the parts as written,
        # where the sample has one.
        for a, template in self.texts:
            if a.type not in (tpl.type, "*") or tpl.base:
                continue
            for container, key, _owner in _find(obj, a.path):
                part = container[key]
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = _said(part, template)
        wrapper = copy.deepcopy(tpl.wrapper) if tpl.wrapper is not None else None
        if wrapper is not None:
            _entry_wrapper(wrapper, tpl.type, obj)
        return Item(tpl.type, tpl.base, obj, wrapper, tpl, list(tpl.holes))

    @staticmethod
    def _code(tpl: Tpl, path: str, element: str, value: str) -> str:
        """The value as FHIR codes it here, where the profile has codes for it."""
        for c in tpl.codes:
            a = at(c["at"])
            if a.type in (tpl.type, "*") and a.path == path and c["element"] == element:
                return str(c["map"].get(value.strip(), c.get("default", value)))
        return value

    # -- places, for derive rules ----------------------------------------------

    def _rel(self, item: Item, where: str) -> str | None:
        a = at(where)
        if a.type not in (item.type, "*"):
            return None
        if not item.base:
            return a.path
        return a.path[len(item.base) + 1:] if a.path.startswith(item.base + ".") else None

    def matches(self, item: Item, where: str) -> bool:
        rel = self._rel(item, where)
        return rel is not None and bool(_find(item.obj, rel))

    def get(self, item: Item, where: str) -> Any:
        rel = self._rel(item, where)
        found = _find(item.obj, rel) if rel is not None else []
        if not found:
            return None
        container, key, _ = found[0]
        return container[key]

    def set(self, item: Item, where: str, value: str) -> None:
        rel = self._rel(item, where)
        for container, key, _ in (_find(item.obj, rel) if rel is not None else []):
            container[key] = _typed(value, container[key])

    def original(self, item: Item, where: str) -> Any:
        rel = self._rel(item, where)
        found = _find(item.tpl.obj, rel) if rel is not None else []
        return found[0][0][found[0][1]] if found else None

    def drop(self, item: Item, where: str) -> None:
        rel = self._rel(item, where)
        for container, key, _ in reversed(_find(item.obj, rel) if rel is not None else []):
            del container[key]

    def aliases(self, item: Item, old: str, new: str) -> dict[str, str]:
        """How other resources refer to this one, before and after: Type/id and its fullUrl."""
        out = {f"{item.type}/{old}": f"{item.type}/{new}"}
        full = (item.wrapper or {}).get("fullUrl")
        if isinstance(full, str):
            if full.startswith("urn:uuid:"):
                fresh = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{full}#{new}')}"
            elif full.endswith(f"{item.type}/{old}"):
                fresh = full[: -len(old)] + new
            else:
                fresh = full
            out[full] = fresh
            item.wrapper["fullUrl"] = fresh
        request = (item.wrapper or {}).get("request")
        if isinstance(request, dict) and request.get("url") == f"{item.type}/{old}":
            request["url"] = f"{item.type}/{new}"
        return out

    def relink(self, item: Item, mapping: dict[str, str]) -> None:
        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "reference" and isinstance(value, str) and value in mapping:
                        node[key] = mapping[value]
                    else:
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(item.obj)

    # -- writing ---------------------------------------------------------------

    def lines(self, top: engine.Written) -> list[str]:
        """A unit's entries; children repeated inside a resource go into its array."""
        out: list[str] = []

        def place(unit: engine.Written):
            for child in unit.children:
                if child.spec.get("in"):
                    path = child.spec["in"]
                    _array_owner(unit.items[0].obj, path).setdefault(
                        _steps(path)[-1][0], []).extend(i.obj for i in child.items)
            for item in unit.items:
                if not item.base:
                    out.append(self.render(item))
            for child in unit.children:
                if not child.spec.get("in"):
                    place(child)

        place(top)
        return out

    def render(self, item: Item) -> str:
        entry = {**(item.wrapper or {}), "resource": item.obj}
        text = json.dumps(entry, indent=2, ensure_ascii=False)
        text = "\n".join("    " + line for line in text.splitlines())
        lead = "" if self.first else ",\n"
        self.first = False
        return lead + text

    def envelope(self, sample: Sample) -> tuple[list[str], list[str]]:
        before, _after = self._shell(sample)
        return [before], []

    def closing(self, sample: Sample, count: int) -> list[str]:
        self.count = count          # the entries written, for a Bundle's total
        return []

    def after(self, sample: Sample) -> list[str]:
        return [self._shell(sample)[1]]

    def _shell(self, sample: Sample) -> tuple[str, str]:
        """The sample's Bundle around its entries: the text before them and after."""
        shell = _bundle_shell(sample.bundle, self.seed)
        shell["entry"] = [_PLACEHOLDER]
        if "total" in sample.bundle and self.count is not None:
            # What the file holds, not what the sample did.
            shell["total"] = self.count
        text = json.dumps(shell, indent=2, ensure_ascii=False)
        token = json.dumps(_PLACEHOLDER)
        start = text.index(token)
        line_start = text.rindex("\n", 0, start) + 1
        return text[:line_start], "\n" + text[start + len(token):].lstrip(" ").lstrip("\n")

    def join(self, lines: list[str]) -> str:
        return "".join(lines)


def _entry_wrapper(wrapper: dict[str, Any], resource_type: str, obj: dict[str, Any]) -> None:
    """An entry's wrapper without the sample's own identifiers: a conditional
    request's search and headers, and a response's location and versions."""
    request = wrapper.get("request")
    if isinstance(request, dict):
        for key in _REQUEST_DROPPED:
            request.pop(key, None)
        url = request.get("url")
        if isinstance(url, str) and "?" in url:
            # A conditional url searches by the sample's identifiers; the
            # resource written is addressed by its own id instead.
            method = str(request.get("method") or "").upper()
            request["url"] = (f"{resource_type}/{obj['id']}"
                              if method in ("PUT", "PATCH", "DELETE") and obj.get("id")
                              else resource_type)
    response = wrapper.get("response")
    if isinstance(response, dict):
        wrapper["response"] = {k: v for k, v in response.items() if k == "status"}


def _vary(obj: dict[str, Any], path: str, vary, values, row: int) -> None:
    """Leave a field out, or write its array as many times as drawn: a copy
    past the sample's takes the sample's elements in turn."""
    steps = _steps(path)
    name, many = steps[-1]
    owners: list[dict] = []

    def walk(node: Any, rest) -> None:
        if not rest:
            if isinstance(node, dict):
                owners.append(node)
            return
        (step, step_many), more = rest[0], rest[1:]
        if not isinstance(node, dict) or step not in node:
            return
        value = node[step]
        for element in (value if step_many and isinstance(value, list) else [value]):
            walk(element, more)

    walk(obj, steps[:-1])
    for place, owner in enumerate(owners):
        if name not in owner:
            continue
        value = owner[name]
        listed = many and isinstance(value, list)
        if listed and not value:
            continue
        n = values.times(vary, row, place, len(value) if listed else 1)
        if n == 0:
            del owner[name]
        elif listed:
            owner[name] = [copy.deepcopy(value[i % len(value)]) for i in range(n)]


def structure(sample: Sample) -> list[dict[str, Any]]:
    """The places a config can vary: each field of the sample's resources that
    holds an object or an array, with the most times the sample has it."""
    seen: dict[str, int] = {}

    def walk(node: Any, kind: str, path: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in ("resourceType", "id", "meta", "text", "contained"):
                continue
            here = f"{path}.{key}" if path else key
            if isinstance(value, list):
                place = f"{kind} {here}[*]"
                seen[place] = max(seen.get(place, 0), len(value))
                for element in value:
                    walk(element, kind, here + "[*]")
            elif isinstance(value, dict):
                place = f"{kind} {here}"
                seen[place] = max(seen.get(place, 0), 1)
                walk(value, kind, here)
            elif not path:
                seen[f"{kind} {here}"] = max(seen.get(f"{kind} {here}", 0), 1)

    for e in sample.entries:
        walk(e["resource"], e["resource"]["resourceType"], "")
    # What a unit repeats (a Claim's items) is as many as the records have.
    counted = set()

    def units(specs, kinds: str) -> None:
        for spec in specs or []:
            if spec.get("in"):
                counted.update(f"{k} {spec['in']}" for k in kinds.split("|"))
            units(spec.get("units"), spec.get("starts") or kinds)

    units(sample.profile.get("units"), "*")
    return [{"at": k, "count": n, "repeats": k.endswith("[*]")} for k, n in seen.items()
            if k not in counted]


def _without(obj: dict[str, Any], path: str) -> dict[str, Any]:
    """A copy of obj with the array at path taken out (a child unit fills it)."""
    out = copy.deepcopy(obj)
    _array_owner(out, path).pop(_steps(path)[-1][0], None)
    return out


def _array_owner(obj: dict[str, Any], path: str) -> dict[str, Any]:
    """The object holding the array a path ends in ('item[*]' -> the resource)."""
    node: Any = obj
    for name, _many in _steps(path)[:-1]:
        node = node.setdefault(name, {})
    return node


def stream(
    sample: Sample,
    entries: list[Entry],
    records: Iterable[dict[str, Any]],
    rules: dict[str, dict],
    *,
    seed: str = "fhir",
    as_of: str = "2026-06-30",
    vary: Iterable[engine.Vary] = (),
) -> Iterator[str]:
    """The sample's Bundle, its units repeated for these records."""
    yield from engine.stream(Reader(sample, seed), sample, entries, records, rules,
                             seed=seed, as_of=as_of, vary=vary)


# ---------------------------------------------------------------------------
# Checking a written file
# ---------------------------------------------------------------------------

DISCLAIMER = ("Structure is checked against the FHIR R4B models (fhir.resources), which R4 "
              "Patient, Coverage, Claim and ExplanationOfBenefit match; value sets are checked "
              "only where the profile lists their codes. Not a conformance test against an "
              "implementation guide (CARIN Blue Button, US Core).")


def _level(level: int, name: str, rules: int, findings: list[dict], notes=()) -> dict[str, Any]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    return {"level": level, "name": name, "rules": rules, "errors": errors,
            "warnings": warnings, "notes": list(notes), "findings": findings,
            "status": "fail" if errors else ("warn" if warnings else "pass")}


def _finding(level: int, rule: str, message: str, where: str,
             severity: str = "error") -> dict[str, Any]:
    return {"level": level, "rule": rule, "message": message, "raw": where, "severity": severity}


def check(text: str, prof: dict[str, Any]) -> dict[str, Any]:
    """A written file checked three ways: structure, references, coded values."""
    deep = (f"Nested deeper than {MAX_DEPTH} levels, which no FHIR resource is.",
            "The file nests too deeply to be FHIR.")
    try:
        doc = json.loads(text)
        problem = deep if _too_deep(doc) else None
    except ValueError as exc:
        problem = (f"Not JSON: {exc}", "The file is not JSON.")
    except RecursionError:
        problem = deep
    if problem is not None:
        level = _level(1, "Structure: the file is JSON", 1,
                       [_finding(1, "json", problem[0], "")])
        return {"passed": False, "summary": problem[1], "levels": [level],
                "disclaimer": DISCLAIMER}
    resources = [e.get("resource") or {} for e in doc.get("entry") or []]

    # 1. Structure: every field known, of its type, required ones present.
    structure: list[dict] = []
    try:
        from fhir.resources.R4B.bundle import Bundle
    except ImportError:
        notes = ["fhir.resources is not installed: structure not checked."]
        levels = [_level(1, "Structure (FHIR models)", 0, [], notes)]
        levels[0]["status"] = "not_checked"
    else:
        from pydantic import ValidationError

        try:
            Bundle.model_validate(doc)
        except ValidationError as exc:
            for err in exc.errors()[:200]:
                where = ".".join(str(p) for p in err.get("loc") or ())
                structure.append(_finding(1, err.get("type") or "invalid", err.get("msg") or "",
                                          where))
        levels = [_level(1, "Structure (FHIR models: fields, types, required)", 1, structure)]

    # 2. References: each resource once, every reference to one in the Bundle.
    refs: list[dict] = []
    seen: dict[str, int] = {}
    urls = {e.get("fullUrl") for e in doc.get("entry") or [] if e.get("fullUrl")}
    for r in resources:
        key = f"{r.get('resourceType')}/{r.get('id')}"
        seen[key] = seen.get(key, 0) + 1
    for key, n in seen.items():
        if n > 1:
            refs.append(_finding(2, "id-unique", f"{key} appears {n} times.", key))
    for r in resources:
        for path, container, k, _owner in _leaves(r):
            if not path.endswith("reference") or not isinstance(container[k], str):
                continue
            target = container[k]
            if target in urls or target in seen:
                continue
            if "://" in target and not any(target.endswith(f"/{s}") for s in seen):
                continue        # outside this Bundle by design (a payer's own server)
            # FHIR lets a reference resolve on a server rather than in the
            # Bundle, so this is said, not failed (the sample may do it too).
            refs.append(_finding(2, "reference-resolves",
                                 f"{r.get('resourceType')}.{path} points at {target}, "
                                 "which is not in the Bundle.",
                                 f"{r.get('resourceType')}/{r.get('id')}", "warning"))
    levels.append(_level(2, "References resolve; ids unique", 2, refs))

    # 3. Coded values: in the codes the profile lists for the place.
    codes: list[dict] = []
    lists = list((prof.get("checks") or {}).get("codes") or [])
    for rule in lists:
        a = at(rule["at"])
        allowed = set(rule["in"])
        for r in resources:
            if a.type not in (r.get("resourceType"), "*"):
                continue
            for container, k, _owner in _find(r, a.path):
                if container[k] not in allowed:
                    codes.append(_finding(3, f"code:{a.path}",
                                          f"{rule['at']} is {container[k]!r}; FHIR allows "
                                          f"{', '.join(sorted(allowed))}.",
                                          f"{r.get('resourceType')}/{r.get('id')}"))
    levels.append(_level(3, "Coded values in their value sets", len(lists), codes))

    passed = not any(lv["errors"] for lv in levels)
    summary = (f"{len(resources):,} resources in a {doc.get('type') or ''} Bundle; "
               f"{'no errors' if passed else 'errors found'}.")
    return {"passed": passed, "summary": summary, "levels": levels, "disclaimer": DISCLAIMER}
