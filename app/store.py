"""SQLite 持久化：runs 存巡检批次，notices / notice_revisions 存整改通知。

表设计说明：
- batch_key 唯一，用来挡住同一输入的重复入库（幂等重跑的直接依据）；
- run_id 由 batch_key 派生，同输入必然同号，重跑不产生新行；
- 明细（目录行、收录行、探测、缺陷、隔离）整包进 payload，导出与对账同源；
- 巡检、整改跟踪、通知依据分开保存：runs 是巡检证据，notices 是出单时的
  原始依据（建行后不改），notice_revisions 是每次新巡检追加的复核记录；
- notices.key 唯一：同一地址只保留一个通知编号，重试出单不会重复；
- notice_revisions (notice_no, run_id) 唯一：同一批次重复复核只留一条。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    batch_key     TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL,
    input_digest  TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    sites         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    elapsed_ms    INTEGER DEFAULT 0,
    findings_total INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS notices (
    notice_no     TEXT PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    site          TEXT NOT NULL,
    url           TEXT NOT NULL,
    category      TEXT NOT NULL,
    opened_run_id TEXT NOT NULL,
    opened_at     TEXT NOT NULL,
    basis         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notices_site ON notices(site);

CREATE TABLE IF NOT EXISTS notice_revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_no  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    detail     TEXT NOT NULL,
    UNIQUE(notice_no, run_id)
);
CREATE INDEX IF NOT EXISTS idx_notice_revisions_notice ON notice_revisions(notice_no, id);
"""

SUMMARY_FIELDS = ("run_id", "batch_key", "status", "input_digest", "rule_version",
                  "started_at", "finished_at", "elapsed_ms", "findings_total")


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(os.path.dirname(self.path) or ".").mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- 写入 ---------------------------------------------------------------
    def save_run(self, run: dict) -> dict:
        payload = json.dumps(run, ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """INSERT INTO runs (run_id, batch_key, status, input_digest, rule_version,
                                 sites, started_at, finished_at, elapsed_ms,
                                 findings_total, updated_at, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 status=excluded.status, finished_at=excluded.finished_at,
                 elapsed_ms=excluded.elapsed_ms, findings_total=excluded.findings_total,
                 updated_at=excluded.updated_at, payload=excluded.payload""",
            (run["run_id"], run["batch_key"], run["status"], run["input_digest"],
             run["rule_version"], ",".join(run["sites"]), run["started_at"],
             run.get("finished_at"), int(run.get("elapsed_ms") or 0),
             int(run.get("totals", {}).get("findings_total") or 0),
             run.get("finished_at") or run["started_at"], payload),
        )
        self._conn.commit()
        return run

    # -- 读取 ---------------------------------------------------------------
    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def find_by_batch_key(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT payload FROM runs WHERE batch_key = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_run(self) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, batch_key, status, input_digest, rule_version, started_at,"
            " finished_at, elapsed_ms, findings_total FROM runs"
            " ORDER BY started_at DESC, run_id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def count_runs(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    # -- 整改通知：依据（notices，建行后不改） -------------------------------
    def create_notice(self, notice: dict) -> bool:
        """出单：key 冲突时不覆盖，返回 False（重试不重复出单的直接依据）。"""
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO notices
                 (notice_no, key, site, url, category, opened_run_id, opened_at, basis)
               VALUES (?,?,?,?,?,?,?,?)""",
            (notice["notice_no"], notice["key"], notice["site"], notice["url"],
             notice["category"], notice["opened_run_id"], notice["opened_at"],
             json.dumps(notice["basis"], ensure_ascii=False, sort_keys=True)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def _notice_row(self, row) -> dict:
        return {"notice_no": row["notice_no"], "key": row["key"], "site": row["site"],
                "url": row["url"], "category": row["category"],
                "opened_run_id": row["opened_run_id"], "opened_at": row["opened_at"],
                "basis": json.loads(row["basis"])}

    def get_notice(self, notice_no: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM notices WHERE notice_no = ?",
                                 (notice_no,)).fetchone()
        return self._notice_row(row) if row else None

    def notice_by_key(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM notices WHERE key = ?", (key,)).fetchone()
        return self._notice_row(row) if row else None

    def list_notices(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM notices ORDER BY notice_no").fetchall()
        return [self._notice_row(row) for row in rows]

    def next_notice_no(self, prefix: str = "ZG") -> str:
        """顺序编号：取现有最大号 +1，出单按 key 排序调用，编号稳定。"""
        row = self._conn.execute(
            "SELECT notice_no FROM notices ORDER BY notice_no DESC LIMIT 1").fetchone()
        serial = int(row["notice_no"].rsplit("-", 1)[1]) + 1 if row else 1
        return f"{prefix}-{serial:04d}"

    def count_notices(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0])

    # -- 整改跟踪：修订记录（notice_revisions，只追加） -----------------------
    def add_notice_revision(self, revision: dict) -> bool:
        """追加复核记录：(notice_no, run_id) 已存在时不重复，返回 False。"""
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO notice_revisions
                 (notice_no, run_id, checked_at, outcome, detail)
               VALUES (?,?,?,?,?)""",
            (revision["notice_no"], revision["run_id"], revision["checked_at"],
             revision["outcome"],
             json.dumps(revision["detail"], ensure_ascii=False, sort_keys=True)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_notice_revisions(self, notice_no: str | None = None) -> list[dict]:
        if notice_no:
            rows = self._conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_no = ? ORDER BY id",
                (notice_no,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM notice_revisions ORDER BY id").fetchall()
        return [{"id": row["id"], "notice_no": row["notice_no"], "run_id": row["run_id"],
                 "checked_at": row["checked_at"], "outcome": row["outcome"],
                 "detail": json.loads(row["detail"])} for row in rows]

    def close(self) -> None:
        self._conn.close()
