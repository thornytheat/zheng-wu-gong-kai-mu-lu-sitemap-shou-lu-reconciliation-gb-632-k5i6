"""M8 整改通知：复核结论的纯规则，不读库、不发请求，可单测。

三条结论互斥且穷尽：
1. 仍有故障：本次巡检该地址仍在册（目录或收录至少一侧还有），探测仍有缺陷；
2. 实测恢复：本次巡检该地址仍在册，探测已对平（分类 OK）；
3. 来源移除：本次巡检的在册范围里已经没有这个地址——既不在目录也不在收录。
   来源移除不算已修复：通知仍在办，等人工确认是整改撤下还是漏报目录。

当前结论只由修订记录推导：没有修订记录时是「待复核」；
通知依据（basis）在出单时定稿，复核只追加修订记录，不改依据。
"""
from __future__ import annotations

OUTCOME_PENDING = "pending"
OUTCOME_STILL_FAILING = "still_failing"
OUTCOME_SOURCE_REMOVED = "source_removed"
OUTCOME_RECOVERED = "recovered"
OUTCOMES = (OUTCOME_PENDING, OUTCOME_STILL_FAILING, OUTCOME_SOURCE_REMOVED,
            OUTCOME_RECOVERED)

STATUS_OPEN = "open"
STATUS_RECOVERED = "recovered"


def probe_index(run: dict) -> dict[str, dict]:
    """本次巡检的在册范围：比较键 → 探测行。不在索引里的地址即不在册。"""
    return {row["key"]: row for row in run.get("probes") or []}


def assess(key: str, index: dict[str, dict]) -> dict:
    """对一个地址下复核结论：在册按探测分类，不在册记来源移除。"""
    row = index.get(key)
    if row is None:
        return {"outcome": OUTCOME_SOURCE_REMOVED,
                "detail": {"probed": False, "in_catalog": 0, "in_sitemap": 0}}
    detail = {"probed": True, "status": row.get("status"), "hop_count": row.get("hop_count"),
              "category": row.get("category"), "category_label": row.get("category_label", ""),
              "reason": row.get("reason") or "", "in_catalog": row.get("in_catalog", 0),
              "in_sitemap": row.get("in_sitemap", 0), "final_url": row.get("final_url", "")}
    if row.get("category") == "OK":
        return {"outcome": OUTCOME_RECOVERED, "detail": detail}
    return {"outcome": OUTCOME_STILL_FAILING, "detail": detail}


def status_of(outcome: str) -> str:
    """只有实测恢复才办结；待复核、仍有故障、来源移除都仍在办。"""
    return STATUS_RECOVERED if outcome == OUTCOME_RECOVERED else STATUS_OPEN


def current_state(revisions: list[dict]) -> tuple[str, str]:
    """由修订记录推当前结论与状态；没有修订记录即待复核。"""
    if not revisions:
        return OUTCOME_PENDING, STATUS_OPEN
    outcome = revisions[-1]["outcome"]
    return outcome, status_of(outcome)
