"""M8 编排：出单、复核、台账与导出取数。

出单与复核都幂等：
- 出单按地址比较键去重，同一地址只保留一个通知编号，重试不重复出单；
- 复核对同一批次只追加一次修订记录（(notice_no, run_id) 唯一），
  新探测只追加修订记录，不覆盖通知原始依据。

复核结论只由修订记录推导，通知行本身不存会变的结论字段。
"""
from __future__ import annotations

from ... import errors, manifest, reasons
from . import tracking


def _require_run(store, run_id: str | None) -> dict:
    run = store.get_run(run_id) if run_id else store.latest_run()
    if run is None:
        raise errors.NotFound("还没有巡检批次，先跑一次巡检", code="E_NO_RUN")
    return run


def _require_probes(run: dict) -> list[dict]:
    probes = run.get("probes") or []
    if not probes:
        raise errors.BadRequest(f"批次 {run['run_id']} 没有探测结果，不能用于出单或复核",
                                code="E_NO_PROBES")
    return probes


def issue(store, run_id: str | None = None, site: str | None = None,
          category: str | None = None) -> dict:
    """出单：对一批巡检的缺陷地址开整改通知，已出单的地址自动跳过。"""
    run = _require_run(store, run_id)
    probes = _require_probes(run)
    if category and category not in reasons.CATEGORIES:
        raise errors.BadRequest(f"未知分类 {category}", code="E_BAD_CATEGORY")
    defects = [row for row in probes if row["category"] != "OK"]
    if site:
        defects = [row for row in defects if row["site"] == site]
    if category:
        defects = [row for row in defects if row["category"] == category]
    defects.sort(key=lambda row: row["key"])  # 全序，编号与出单顺序可复算

    opened_at = manifest.now_utc()
    created: list[str] = []
    reused = 0
    for row in defects:
        if store.notice_by_key(row["key"]) is not None:
            reused += 1
            continue
        notice = {
            "notice_no": store.next_notice_no(),
            "key": row["key"],
            "site": row["site"],
            "url": row["url"],
            "category": row["category"],
            "opened_run_id": run["run_id"],
            "opened_at": opened_at,
            "basis": _basis(run, row),
        }
        if store.create_notice(notice):
            created.append(notice["notice_no"])
        else:
            reused += 1  # 并发或重试撞上唯一键，按复用计
    return {"run_id": run["run_id"], "defects": len(defects),
            "created": len(created), "reused": reused,
            "notice_nos": created, "total": store.count_notices()}


def _basis(run: dict, row: dict) -> dict:
    """通知原始依据：出单那一刻的探测证据，之后任何复核都不改写。"""
    evidence = row.get("evidence") or {}
    return {"run_id": run["run_id"], "category": row["category"],
            "category_label": reasons.label(row["category"]),
            "status": row.get("status"), "hop_count": row.get("hop_count"),
            "reason": row.get("reason") or "",
            "reason_label": row.get("reason_label", ""),
            "in_catalog": evidence.get("in_catalog", row.get("in_catalog", 0)),
            "in_sitemap": evidence.get("in_sitemap", row.get("in_sitemap", 0)),
            "raw_forms": list(evidence.get("raw_forms") or []),
            "notes": list(evidence.get("notes") or [])}


def recheck(store, run: dict) -> dict:
    """复核：用一次新巡检对所有通知各追加一条修订记录。

    出单批次本身跳过（依据就是那批的证据，复核要有新探测）；
    没有探测结果的批次不能复核（部分完成的批次不在在册范围内下结论）。
    """
    probes = run.get("probes") or []
    if not probes:
        return {"run_id": run.get("run_id", ""), "checked": 0, "appended": 0,
                "skipped": 0, "note": "E_NO_PROBES"}
    index = tracking.probe_index(run)
    checked_at = manifest.now_utc()
    appended = skipped = 0
    for notice in store.list_notices():
        if notice["opened_run_id"] == run["run_id"]:
            skipped += 1
            continue
        verdict = tracking.assess(notice["key"], index)
        revision = {"notice_no": notice["notice_no"], "run_id": run["run_id"],
                    "checked_at": checked_at, "outcome": verdict["outcome"],
                    "detail": verdict["detail"]}
        if store.add_notice_revision(revision):
            appended += 1
        else:
            skipped += 1  # 同一批次已复核过，重试不重复追加
    return {"run_id": run["run_id"], "checked": appended + skipped,
            "appended": appended, "skipped": skipped, "note": ""}


def _row(notice: dict, revisions: list[dict]) -> dict:
    outcome, status = tracking.current_state(revisions)
    return {**notice,
            "category_label": reasons.label(notice["category"]),
            "assessment": outcome,
            "assessment_label": reasons.notice_outcome_label(outcome),
            "assessment_level": reasons.notice_outcome_level(outcome),
            "assessment_note": reasons.notice_outcome_note(outcome),
            "status": status,
            "status_label": reasons.notice_status_label(status),
            "revisions": len(revisions),
            "last_checked_at": revisions[-1]["checked_at"] if revisions else "",
            "last_run_id": revisions[-1]["run_id"] if revisions else ""}


def _grouped_revisions(store) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for revision in store.list_notice_revisions():
        grouped.setdefault(revision["notice_no"], []).append(revision)
    return grouped


def list_notices(store, site: str | None = None, assessment: str | None = None,
                 status: str | None = None) -> dict:
    if assessment and assessment not in reasons.NOTICE_OUTCOMES:
        raise errors.BadRequest(f"未知结论 {assessment}", code="E_BAD_ASSESSMENT")
    if status and status not in reasons.NOTICE_STATUS:
        raise errors.BadRequest(f"未知状态 {status}", code="E_BAD_STATUS")
    grouped = _grouped_revisions(store)
    rows = [_row(notice, grouped.get(notice["notice_no"], []))
            for notice in store.list_notices()]
    if site:
        rows = [row for row in rows if row["site"] == site]
    if assessment:
        rows = [row for row in rows if row["assessment"] == assessment]
    if status:
        rows = [row for row in rows if row["status"] == status]
    rows.sort(key=lambda row: (reasons.notice_outcome_rank(row["assessment"]),
                               row["site"], row["notice_no"]))
    return {"total": store.count_notices(), "count": len(rows), "rows": rows,
            "ledger": ledger(store), "outcomes": reasons.NOTICE_OUTCOMES,
            "statuses": reasons.NOTICE_STATUS}


def detail(store, notice_no: str) -> dict:
    notice = store.get_notice(notice_no)
    if notice is None:
        raise errors.NotFound(f"通知 {notice_no} 不存在", code="E_NO_NOTICE")
    revisions = store.list_notice_revisions(notice_no)
    row = _row(notice, revisions)
    for revision in revisions:
        revision["outcome_label"] = reasons.notice_outcome_label(revision["outcome"])
        revision["outcome_level"] = reasons.notice_outcome_level(revision["outcome"])
    return {**row, "revision_rows": revisions}


def ledger(store) -> dict:
    """对账用台账：按结论分桶的在办/已恢复合计，页面与 API 同源。"""
    grouped = _grouped_revisions(store)
    counts = {outcome: 0 for outcome in reasons.NOTICE_OUTCOMES}
    open_count = recovered_count = 0
    for notice in store.list_notices():
        outcome, status = tracking.current_state(grouped.get(notice["notice_no"], []))
        counts[outcome] += 1
        if status == tracking.STATUS_RECOVERED:
            recovered_count += 1
        else:
            open_count += 1
    total = open_count + recovered_count
    return {"total": total, "open": open_count, "recovered": recovered_count,
            "by_outcome": counts,
            "outcome_labels": {key: item["label"]
                               for key, item in reasons.NOTICE_OUTCOMES.items()}}


def url_map(store) -> dict[str, dict]:
    """地址 → 通知摘要：缺陷单页面给已有通知的地址打标记用。"""
    grouped = _grouped_revisions(store)
    out: dict[str, dict] = {}
    for notice in store.list_notices():
        outcome, _status = tracking.current_state(grouped.get(notice["notice_no"], []))
        out[notice["url"]] = {"notice_no": notice["notice_no"], "assessment": outcome,
                              "assessment_label": reasons.notice_outcome_label(outcome),
                              "assessment_level": reasons.notice_outcome_level(outcome)}
    return out
