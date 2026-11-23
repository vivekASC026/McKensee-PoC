"""In-memory stand-in for sdg.analyzer.db, matching its real semantics.

The point is to exercise save / discover / retrieve / remove against something
that behaves like SQL Server without needing SQL Server: dedup by the exact
pattern_key the real upsert uses, and saved_by / saved_at set and cleared the
same way. If a bug reproduces here, it reproduces in production.
"""

import json
from datetime import datetime


def _make_pattern_key(combo, cluster_id):
    parts = [
        str(combo.get("grgr_ck") or "ALL"),
        str(combo.get("cspd_cat") or "ALL"),
        str(combo.get("cspi_id") or "ALL"),
        str(combo.get("pdpd_id") or "ALL"),
        str(cluster_id),
    ]
    return "|".join(parts)


class FakeDB:
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.by_key: dict[str, int] = {}
        self._next = 1

    def upsert_pattern(self, combo, cluster_id, contextual_name, member_count,
                       pct_of_pop, silhouette, profile=None, ai_summary=None,
                       conn=None, pattern_key=None):
        # `conn` is accepted and ignored: the real function takes it so a walk
        # can reuse one connection. The stub must track that signature or it
        # silently diverges from the thing it stands in for. `pattern_key` is
        # honored, as the real one does for a saved pattern.
        key = pattern_key or _make_pattern_key(combo, cluster_id)
        if key in self.by_key:
            pid = self.by_key[key]
            self.rows[pid].update({
                "contextual_name": contextual_name,
                "member_count": member_count,
                "pct_of_pop": pct_of_pop,
                "silhouette": silhouette,
                "profile_json": json.dumps(profile, default=str) if profile else None,
            })
            return pid

        pid = self._next
        self._next += 1
        self.by_key[key] = pid
        self.rows[pid] = {
            "id": pid, "pattern_key": key, "contextual_name": contextual_name,
            "grgr_ck": json.dumps(combo.get("grgr_ck")),
            "cspd_cat": json.dumps(combo.get("cspd_cat")),
            "cspi_id": json.dumps(combo.get("cspi_id")),
            "pdpd_id": json.dumps(combo.get("pdpd_id")),
            "grgr_name": combo.get("grgr_name", ""),
            "cspd_cat_desc": combo.get("cspd_cat_desc", ""),
            "plds_desc": combo.get("plds_desc", ""),
            "pdds_desc": combo.get("pdds_desc", ""),
            "cluster_id": cluster_id, "member_count": member_count,
            "pct_of_pop": pct_of_pop, "silhouette": silhouette,
            "profile_json": json.dumps(profile, default=str) if profile else None,
            "saved_by": None, "saved_at": None,
        }
        return pid

    def pattern_id_for_key(self, pattern_key):
        return self.by_key.get(pattern_key)

    def get_pattern(self, pattern_id):
        return self.rows.get(pattern_id)

    def get_all_patterns(self):
        return sorted(self.rows.values(),
                      key=lambda r: r["member_count"], reverse=True)

    def mark_pattern_saved(self, pattern_id, saved_by="user"):
        if pattern_id in self.rows:
            self.rows[pattern_id]["saved_by"] = saved_by
            self.rows[pattern_id]["saved_at"] = datetime.now()

    def mark_pattern_unsaved(self, pattern_id):
        if pattern_id in self.rows:
            self.rows[pattern_id]["saved_by"] = None
            self.rows[pattern_id]["saved_at"] = None

    def get_saved_patterns(self):
        saved = [r for r in self.rows.values() if r["saved_by"] is not None]
        return sorted(saved, key=lambda r: r["saved_at"] or datetime.min, reverse=True)

    def is_pattern_saved(self, pattern_id):
        row = self.rows.get(pattern_id)
        return bool(row and row["saved_by"] is not None)

    def save_pattern_from_profiler(self, combo, cluster_id, contextual_name,
                                   member_count, pct_of_pop, silhouette,
                                   profile=None, ai_summary=None, saved_by="user"):
        pid = self.upsert_pattern(combo, cluster_id, contextual_name,
                                  member_count, pct_of_pop, silhouette,
                                  profile, ai_summary)
        if pid:
            self.mark_pattern_saved(pid, saved_by)
        return pid
