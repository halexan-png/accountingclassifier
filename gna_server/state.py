"""state.py — small in-memory server state shared across routes.

Not a database: this is a single-operator, single-machine, single-process
app, and every value here is re-derivable from disk (we just cache the last
upload/validation) so the workbook itself lives on disk in workspace/ and the
operator re-uploads or re-picks it after a restart. Lost on restart; that is
fine.

Thread-safe: read/written from request-handler threads (uploads, quarters)
and from the run-manager's worker thread (reading workbook_path at
run-start).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class ServerState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.workbook_path: Path | None = None
        self.workbook_checks: list[dict[str, Any]] = []
        self.workbook_row_count: int = 0

    def set_workbook(
        self,
        path: Path,
        *,
        checks: list[dict[str, Any]],
        row_count: int,
    ) -> None:
        with self._lock:
            self.workbook_path = path
            self.workbook_checks = checks
            self.workbook_row_count = row_count

    def workbook_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self.workbook_path is None:
                return None
            try:
                mtime = self.workbook_path.stat().st_mtime
            except OSError:
                mtime = None
            return {"name": self.workbook_path.name, "mtime": mtime}


state = ServerState()
