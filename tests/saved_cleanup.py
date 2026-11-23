"""Leave the Saved list as a test found it.

A save returns `created` when it made the row: the test deletes those. A
pattern somebody saved and then deleted from Saved keeps its row (so Undo can
bring it back), and saving it again restores it -- `restored`, not `created`.
That row is not the test's to delete, but the test must put it back as it was:
unsaved.
"""

from __future__ import annotations

from typing import Iterable


def unrestore(saves: Iterable[dict]) -> None:
    """Unsave again every pattern these saves restored."""
    from sdg.analyzer import db

    for save in saves:
        if save.get("restored"):
            db.mark_pattern_unsaved(int(save["pattern_id"]))
