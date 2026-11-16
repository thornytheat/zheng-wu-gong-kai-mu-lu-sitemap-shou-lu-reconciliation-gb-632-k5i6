"""整改通知导出：CSV 带 BOM，列顺序固定，每条记录标明复核结论。

结论列只取四个稳定值：待复核 / 仍有故障 / 来源移除 / 实测恢复；
来源移除不算已修复，状态列仍是「在办」。
"""
from __future__ import annotations

import csv
import io

from ... import manifest

COLUMNS = ("通知编号", "站点", "分类", "地址", "复核结论", "状态", "出单批次",
           "出单时间", "复核次数", "最近复核")

BOM = "\ufeff"


def notices_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([
            row["notice_no"], row["site"], row["category_label"], row["url"],
            row["assessment_label"], row["status_label"], row["opened_run_id"],
            manifest.local_stamp(row["opened_at"]), row["revisions"],
            manifest.local_stamp(row["last_checked_at"]) if row["last_checked_at"] else "—",
        ])
    return BOM + buffer.getvalue()


def filename() -> str:
    return "notices.csv"
