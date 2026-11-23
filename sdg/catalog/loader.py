"""Load the element catalog from YAML.

Dictionaries are kept as separate files rather than inline in the catalog, so a
list of six hundred postal codes does not drown the rules. A `dictionary:`
parameter is expanded into `values` at load time, and the element that the plan
and the engine see is self-contained.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from sdg.catalog.model import Catalog, Element
from sdg import packs
from sdg.config import REPO_ROOT

DEFAULT_DICTIONARIES = REPO_ROOT / "examples" / "dictionaries"

# A dictionary is named, not addressed: a plain file name in the dictionaries
# folder, never a path that could reach a file elsewhere.
_DICTIONARY_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class CatalogError(ValueError):
    """A catalog that cannot be loaded."""


@lru_cache(maxsize=32)
def load_dictionary(name: str, directory: str | None = None) -> tuple[Any, ...]:
    """Read a dictionary file.

    A .txt file is one value per line. A .csv file with a header becomes a list
    of records, which is what a column set such as ZIP, city, and state needs.
    """
    if not _DICTIONARY_NAME.match(name):
        raise CatalogError(
            f"dictionary name {name!r} is not a plain file name; use letters, digits, "
            f"underscores, dots or hyphens, and keep the file in the dictionaries folder"
        )
    base = Path(directory) if directory else DEFAULT_DICTIONARIES
    text_path = base / f"{name}.txt"
    csv_path = base / f"{name}.csv"

    if text_path.exists():
        lines = text_path.read_text(encoding="utf-8").splitlines()
        return tuple(line.strip() for line in lines if line.strip())

    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            return tuple(dict(row) for row in csv.DictReader(handle))

    raise CatalogError(
        f"dictionary {name!r} not found; looked for {text_path} and {csv_path}"
    )


def expand_dictionaries(element: Element, directory: str | None = None) -> Element:
    """Replace a `dictionary` parameter with the values it names."""
    name = element.params.get("dictionary")
    if not name or "values" in element.params:
        return element
    values = load_dictionary(str(name), directory)
    element.params = {**element.params, "values": list(values)}
    element.params.pop("dictionary", None)
    return element


def load_catalog(
    path: str | Path | None = None, *, dictionaries: str | None = None
) -> Catalog:
    # Unless one is named, the catalog is the business pack's own.
    path = Path(path) if path else packs.active().catalog_path
    if not path.exists():
        raise CatalogError(f"catalog not found: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise CatalogError(f"{path} must hold a mapping at the top level")

    try:
        catalog = Catalog.model_validate(document)
    except Exception as exc:
        raise CatalogError(f"{path}: {exc}") from exc

    for element in catalog.elements:
        expand_dictionaries(element, dictionaries)
    return catalog
