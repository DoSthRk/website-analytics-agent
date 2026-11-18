"""Append-only local confirmation ledger. Reading never creates a database."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from website_analytics.analysis.contracts import AnalysisError, BatchDraft, canonical_json, identifier, text, timestamp


@dataclass(frozen=True)
class ConfirmedBatch:
    draft: BatchDraft
    confirmed_by: str
    confirmed_at: str

    def document(self) -> dict:
        return {"batch": self.draft.document(), "digest": self.draft.digest,
                "confirmed_by": self.confirmed_by, "confirmed_at": self.confirmed_at}

    @classmethod
    def parse(cls, value: object) -> ConfirmedBatch:
        from website_analytics.analysis.contracts import strict_keys
        raw = strict_keys(value, {"batch", "digest", "confirmed_by", "confirmed_at"})
        draft = BatchDraft.parse(raw["batch"])
        if raw["digest"] != draft.digest:
            raise AnalysisError("confirmation ledger integrity mismatch")
        actor = text(raw["confirmed_by"], limit=100)
        at = timestamp(raw["confirmed_at"])
        if not draft.sample and timestamp(draft.published_at) > at:
            raise AnalysisError("actual publication cannot be in the future at confirmation")
        return cls(draft, actor, at.isoformat())


class BatchStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def confirm(self, draft: BatchDraft, expected_digest: str, confirmed_by: str,
                *, confirmed_at: str | None = None) -> ConfirmedBatch:
        draft = BatchDraft.parse(draft.document())
        if draft.digest != expected_digest:
            raise AnalysisError("draft changed since review; confirmation rejected")
        actor = text(confirmed_by, limit=100)
        at = timestamp(confirmed_at or datetime.now(timezone.utc).isoformat()).isoformat()
        if not draft.sample and timestamp(draft.published_at) > timestamp(at):
            raise AnalysisError("actual publication cannot be in the future at confirmation")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS batches (batch_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, previous_digest TEXT, document TEXT NOT NULL, confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY(batch_id, revision))")
            db.execute("BEGIN IMMEDIATE")
            history = _history(db, draft.batch_id)
            previous = (history[-1].draft.revision, history[-1].draft.digest) if history else None
            if (previous is None and draft.revision != 1) or (previous is not None and (draft.revision != previous[0]+1 or draft.previous_digest != previous[1])):
                raise AnalysisError("stale or duplicate revision; existing confirmations are immutable")
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?)", (draft.batch_id, draft.revision, draft.digest, draft.previous_digest, canonical_json(draft.document()), actor, at))
        return ConfirmedBatch(draft, actor, at)

    def get(self, batch_id: str, revision: int) -> ConfirmedBatch:
        identifier(batch_id)
        if type(revision) is not int or revision < 1:
            raise AnalysisError("invalid revision")
        if not self.path.is_file():
            raise AnalysisError("confirmation ledger not found")
        with sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            history = _history(db, batch_id, revision)
            if not history or history[-1].draft.revision != revision:
                raise AnalysisError("confirmed batch revision not found")
        return history[-1]


def _history(db: sqlite3.Connection, batch_id: str, up_to: int = 2**31-1) -> list[ConfirmedBatch]:
    rows = db.execute("SELECT revision,document,digest,confirmed_by,confirmed_at,previous_digest FROM batches WHERE batch_id=? AND revision<=? ORDER BY revision", (batch_id, up_to)).fetchall()
    history = []
    previous = None
    for index, row in enumerate(rows, 1):
        item = ConfirmedBatch.parse({"batch": json.loads(row[1]), "digest": row[2], "confirmed_by": row[3], "confirmed_at": row[4]})
        if row[0] != index or item.draft.revision != index or item.draft.batch_id != batch_id or item.draft.previous_digest != previous or row[5] != previous:
            raise AnalysisError("confirmation history chain integrity mismatch")
        history.append(item)
        previous = item.draft.digest
    return history
