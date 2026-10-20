from __future__ import annotations

import datetime as dt
from pathlib import Path


def report_archive_dir(reports_dir: Path, as_of: dt.date) -> Path:
    """返回某个交易日的报告归档目录。"""
    return reports_dir / as_of.isoformat()
