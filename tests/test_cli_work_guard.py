"""The command line never writes one database's model under another's name."""

from sdg.cli.main import work_belongs_elsewhere, work_dir
from sdg.config import get_settings


def test_work_with_no_record_belongs_to_the_source():
    marker = work_dir() / "working-database.txt"
    saved = marker.read_text(encoding="utf-8") if marker.exists() else None
    try:
        marker.unlink(missing_ok=True)
        assert work_belongs_elsewhere() is None
        settings = get_settings()
        marker.write_text(f"{settings.mssql.server}__{settings.source_db}", encoding="utf-8")
        assert work_belongs_elsewhere() is None
        marker.write_text(f"{settings.mssql.server}__SomeOtherDb", encoding="utf-8")
        assert work_belongs_elsewhere() == f"{settings.mssql.server}__SomeOtherDb"
    finally:
        if saved is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(saved, encoding="utf-8")
