"""A customer-neutral zip carries nothing of another installation.

scripts/package.py --customer-neutral leaves out the notes on one
installation's history and the development plans, ships the placeholder
settings as customer.env.example, and refuses to keep a zip in which any file
holds a word it was told to look for.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import package  # noqa: E402


def test_the_installation_notes_and_plans_are_left_out():
    files = package.tracked()
    kept = package.neutral(files)
    for name in ("STATUS.md", "ENVIRONMENTS.md", "AUDIT.md",
                 "docs/day0-accelerator-phase2-plan.md", "scripts/customer/customer.env.example"):
        assert name in files and name not in kept, name
    assert "scripts/customer/new-customer.env.example" in kept
    assert "docs/SDM-New-Setup-Runbook.docx" in kept and "sdg/config.py" in kept


def test_the_placeholder_settings_name_no_server_or_database():
    text = (ROOT / "scripts" / "customer" / "new-customer.env.example").read_text(encoding="utf-8")
    values = [line.split("=", 1)[1] for line in text.splitlines()
              if line and not line.startswith("#") and "=" in line]
    named = [v for v in values if v and not v.startswith("<")]
    # Only fixed choices, never a place: no server, database, login or secret.
    assert set(named) <= {"customer", "sdm", "1433", "yes", "no", "ODBC Driver 18 for SQL Server",
                          "SDM_NO_GENERATION", "50000000", "60", "work"}, named
    assert "SDG_PROTECTED_DATABASES=<" in text


def test_a_zip_holding_a_forbidden_word_is_found(tmp_path):
    out = tmp_path / "x.zip"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("SDM/a.txt", "server ACME-SQL01 here")
        zf.writestr("SDM/b.txt", "nothing")
    assert package.found_words(out, ["acme-sql01", "OTHER"]) == {"acme-sql01": ["SDM/a.txt"]}
    assert package.found_words(out, ["absent"]) == {}


def test_a_forbidden_word_inside_a_document_or_a_name_is_found(tmp_path):
    import io

    doc = io.BytesIO()
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as inner:
        inner.writestr("word/document.xml", "<w:t>Installed at Acme Health</w:t>")
    out = tmp_path / "x.zip"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("SDM/docs/guide.docx", doc.getvalue())
        zf.writestr("SDM/acme-notes.txt", "nothing")
        zf.writestr("SDM/wide.txt", "made by Initech".encode("utf-16-le"))
    found = package.found_words(out, ["acme", "initech"])
    assert found["acme"] == ["SDM/docs/guide.docx!word/document.xml", "SDM/acme-notes.txt"]
    assert found["initech"] == ["SDM/wide.txt"]
