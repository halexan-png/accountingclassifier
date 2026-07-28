"""persistence.py — crash-safe JSONL append + read-back.

Owns no aggregation math and no Excel; the only job here is durability of
DecisionRecords (`append_record`) and reconstructing the full record history
from disk (`load_all_records`), which `recover` uses to rebuild the Excel at
$0. There is no resume/skip mechanism here — every run re-decides every
in-scope row; a later record for the same row simply supersedes an earlier
one in the JSONL under last-write-wins (consumers that fold by row, like
`cli.cmd_recover`, apply that rule themselves).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from gna_pipeline.contract import DecisionRecord

logger = logging.getLogger(__name__)


def append_record(path: str | Path, record: DecisionRecord) -> None:
    """Append ONE DecisionRecord as a single JSON line, flush + fsync.

    Durable the instant the call returns — a crash loses at most the row in
    flight. Creates parent directories on first write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_all_records(path: str | Path) -> list[DecisionRecord]:
    """Read all DecisionRecords from `path` in file order.

    Tolerates a malformed/truncated trailing line (logs a warning and skips
    it, does not raise). Missing file -> empty list.
    """
    path = Path(path)
    records: list[DecisionRecord] = []
    if not path.exists():
        return records

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "persistence: skipping malformed line %d in %s (likely a "
                "truncated crash-mid-write line)",
                i + 1,
                path,
            )
            continue
        records.append(rec)
    return records
