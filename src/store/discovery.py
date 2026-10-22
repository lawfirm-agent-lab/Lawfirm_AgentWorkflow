"""Separate incremental state; never interprets a page as a docket snapshot."""
import hashlib
import json
from src.store.db import Store


class DiscoveryStore(Store):
    def _initialize(self):
        with self.transaction():
            for sql in (
                "CREATE TABLE IF NOT EXISTS discovery_state (stream TEXT PRIMARY KEY, start_at TEXT NOT NULL, end_at TEXT, next_url TEXT, last_key TEXT, revision INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS discovery_records (stream TEXT NOT NULL, record_id TEXT NOT NULL, modified_at TEXT NOT NULL, content_hash TEXT NOT NULL, raw_json TEXT NOT NULL, PRIMARY KEY(stream, record_id))",
                "CREATE TABLE IF NOT EXISTS discovery_requests (request_id TEXT PRIMARY KEY, attempted_at DOUBLE PRECISION NOT NULL)",
                "CREATE TABLE IF NOT EXISTS discovery_control (id INTEGER PRIMARY KEY, blocked_until DOUBLE PRECISION NOT NULL)",
                "CREATE TABLE IF NOT EXISTS discovery_runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, details TEXT)",
            ):
                self.execute(sql)
            self.execute("INSERT INTO discovery_control VALUES (1,0) ON CONFLICT(id) DO NOTHING")

    def initialize_streams(self, start):
        with self.transaction():
            for stream in ("docket-entries", "opinions"):
                self.execute("INSERT INTO discovery_state(stream,start_at) VALUES (?,?) ON CONFLICT(stream) DO NOTHING", (stream, start))

    def reserve(self, at, request_id, limit):
        with self.transaction():
            suffix = " FOR UPDATE" if self.backend == "postgres" else ""
            control = self.execute("SELECT blocked_until FROM discovery_control WHERE id=1" + suffix).fetchone()
            if at < control["blocked_until"]:
                return False
            count = self.execute("SELECT COUNT(*) AS n FROM discovery_requests WHERE attempted_at>?", (at-86400,)).fetchone()["n"]
            last = self.execute("SELECT MAX(attempted_at) AS t FROM discovery_requests").fetchone()["t"]
            if count >= limit or (last is not None and at-last < 13):
                return False
            self.execute("INSERT INTO discovery_requests VALUES (?,?)", (request_id, at))
            self.execute("DELETE FROM discovery_requests WHERE attempted_at<?", (at-172800,))
            return True

    def block(self, until):
        with self.transaction():
            self.execute("UPDATE discovery_control SET blocked_until=? WHERE id=1", (until,))

    def save_page(self, state, rows, end, next_url, last_key):
        with self.transaction():
            updated = self.execute("UPDATE discovery_state SET start_at=?,end_at=?,next_url=?,last_key=?,revision=revision+1 WHERE stream=? AND revision=?",
                (state["start_at"] if next_url else end, end if next_url else None, next_url,
                 json.dumps(last_key) if next_url and last_key else None, state["stream"], state["revision"]))
            if updated.rowcount != 1:
                raise ValueError("Checkpoint conflict")
            changed = 0
            for row in rows:
                raw = json.dumps(row, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(raw.encode()).hexdigest()
                result = self.execute("INSERT INTO discovery_records VALUES (?,?,?,?,?) ON CONFLICT(stream,record_id) DO UPDATE SET modified_at=excluded.modified_at,content_hash=excluded.content_hash,raw_json=excluded.raw_json WHERE discovery_records.content_hash<>excluded.content_hash",
                    (state["stream"], str(row["id"]), row["date_modified"], digest, raw))
                changed += result.rowcount
            return changed
