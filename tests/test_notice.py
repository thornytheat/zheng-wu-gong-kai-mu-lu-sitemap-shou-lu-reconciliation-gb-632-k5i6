"""M8 整改通知：出单幂等、复核结论、依据与修订记录分离、自动复核。

复核结论只有三种 + 待复核：仍有故障 / 实测恢复 / 来源移除（不计已修复）。
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import shutil
import threading

import pytest

from app import errors, reasons
from app.modules.m5_batches import service as batch_service
from app.modules.m8_notice import service as notice_service
from app.modules.m8_notice import tracking
from fixtures.expected import FINDINGS

ROOT = pathlib.Path(__file__).resolve().parent.parent

REMOVED_URL = "http://www.hz.gov.cn/site-a/attach/2026/report.pdf"


def _second_run(run: dict, drop: set[str] | None = None,
                recover: set[str] | None = None,
                run_id: str = "ra-nextquarter01") -> dict:
    """由真实批次造一份「下一季度」批次：移出一些地址、修复一些地址。"""
    doc = copy.deepcopy(run)
    doc["run_id"] = run_id
    doc["started_at"] = "2027-02-01T02:00:00Z"
    probes = []
    for row in doc["probes"]:
        if drop and row["key"] in drop:
            continue  # 来源移除：目录与收录都不再登记这个地址
        if recover and row["key"] in recover:
            row.update({"category": "OK", "category_label": "已对平", "status": 200,
                        "hop_count": 0, "hops": [], "reason": "", "reason_label": ""})
        probes.append(row)
    doc["probes"] = probes
    return doc


@pytest.fixture()
def issued(settings, store, run):
    result = notice_service.issue(store, run["run_id"])
    assert result["created"] == FINDINGS
    return result


# -- 纯规则 -----------------------------------------------------------------

def test_assess_three_outcomes():
    index = {
        "http://h/a": {"key": "http://h/a", "category": "CATALOG_DEAD", "status": 404,
                       "hop_count": 0, "in_catalog": 1, "in_sitemap": 0},
        "http://h/b": {"key": "http://h/b", "category": "OK", "status": 200,
                       "hop_count": 0, "in_catalog": 1, "in_sitemap": 1},
    }
    assert tracking.assess("http://h/a", index)["outcome"] == "still_failing"
    assert tracking.assess("http://h/b", index)["outcome"] == "recovered"
    gone = tracking.assess("http://h/gone", index)
    assert gone["outcome"] == "source_removed"
    assert gone["detail"]["probed"] is False


def test_source_removed_stays_open_not_recovered():
    """来源移除不计已修复：状态仍在办，只有实测恢复才办结。"""
    assert tracking.status_of("source_removed") == "open"
    assert tracking.status_of("still_failing") == "open"
    assert tracking.status_of("pending") == "open"
    assert tracking.status_of("recovered") == "recovered"
    assert tracking.current_state([]) == ("pending", "open")
    revisions = [{"outcome": "source_removed"}, {"outcome": "still_failing"}]
    assert tracking.current_state(revisions) == ("still_failing", "open")


# -- 出单 -------------------------------------------------------------------

def test_issue_creates_one_notice_per_defect(settings, store, run, issued):
    assert issued["reused"] == 0
    assert issued["total"] == FINDINGS
    notices = store.list_notices()
    assert [row["notice_no"] for row in notices] == [
        f"ZG-{index:04d}" for index in range(1, FINDINGS + 1)]
    keys = [row["key"] for row in notices]
    assert len(keys) == len(set(keys))  # 同一地址只保留一个通知编号
    first = notices[0]
    assert first["basis"]["run_id"] == run["run_id"]
    assert first["basis"]["category"] == first["category"]


def test_issue_retry_does_not_duplicate(settings, store, run, issued):
    again = notice_service.issue(store, run["run_id"])
    assert again["created"] == 0
    assert again["reused"] == FINDINGS
    assert store.count_notices() == FINDINGS
    assert [row["notice_no"] for row in store.list_notices()] == [
        f"ZG-{index:04d}" for index in range(1, FINDINGS + 1)]


def test_issue_filters_by_site_and_category(settings, store, run):
    result = notice_service.issue(store, run["run_id"], site="site-b")
    assert result["created"] == 4
    assert all(row["site"] == "site-b" for row in store.list_notices())
    more = notice_service.issue(store, run["run_id"], category="CATALOG_DEAD")
    # CATALOG_DEAD 共 3 条，site-b 那 1 条已随站点出单，只补 site-a 的 2 条
    assert more["created"] == 2
    assert more["reused"] == 1
    assert store.count_notices() == 6


def test_issue_requires_probes(settings, store, partial_run):
    with pytest.raises(errors.BadRequest):
        notice_service.issue(store, partial_run["run_id"])
    with pytest.raises(errors.NotFound):
        notice_service.issue(store, "ra-not-exist")


# -- 复核 -------------------------------------------------------------------

def test_recheck_marks_three_outcomes(settings, store, run, issued):
    defect_keys = [row["key"] for row in run["probes"] if row["category"] != "OK"]
    drop = {defect_keys[0]}
    recover = {defect_keys[1]}
    nxt = _second_run(run, drop=drop, recover=recover)

    result = notice_service.recheck(store, nxt)
    assert result["appended"] == FINDINGS
    rows = {row["key"]: row for row in notice_service.list_notices(store)["rows"]}
    assert rows[defect_keys[0]]["assessment"] == "source_removed"
    assert rows[defect_keys[0]]["status"] == "open"  # 来源移除仍待复核，不算已修复
    assert rows[defect_keys[1]]["assessment"] == "recovered"
    assert rows[defect_keys[1]]["status"] == "recovered"
    assert rows[defect_keys[2]]["assessment"] == "still_failing"
    assert rows[defect_keys[2]]["status"] == "open"


def test_recheck_same_run_twice_appends_once(settings, store, run, issued):
    nxt = _second_run(run)
    first = notice_service.recheck(store, nxt)
    assert first["appended"] == FINDINGS
    again = notice_service.recheck(store, nxt)
    assert again["appended"] == 0
    assert again["skipped"] == FINDINGS
    assert len(store.list_notice_revisions()) == FINDINGS


def test_recheck_skipped_for_issuing_run(settings, store, run, issued):
    result = notice_service.recheck(store, run)
    assert result["appended"] == 0
    assert result["skipped"] == FINDINGS
    assert store.list_notice_revisions() == []


def test_recheck_without_probes_is_noop(settings, store, run, issued, partial_run):
    result = notice_service.recheck(store, partial_run)
    assert result["appended"] == 0
    assert result["note"] == "E_NO_PROBES"


def test_basis_not_overwritten_by_revisions(settings, store, run, issued):
    notice_no = store.list_notices()[0]["notice_no"]
    before = store.get_notice(notice_no)["basis"]
    nxt = _second_run(run, recover={store.list_notices()[0]["key"]})
    notice_service.recheck(store, nxt)
    after = store.get_notice(notice_no)["basis"]
    assert after == before  # 新探测只追加修订记录，不覆盖通知原始依据
    detail = notice_service.detail(store, notice_no)
    assert detail["assessment"] == "recovered"
    assert detail["basis"]["run_id"] == run["run_id"]
    assert len(detail["revision_rows"]) == 1
    assert detail["revision_rows"][0]["outcome"] == "recovered"


def test_relapse_reuses_same_notice_number(settings, store, run, issued):
    key = store.list_notices()[0]["key"]
    notice_service.recheck(store, _second_run(run, recover={key},
                                              run_id="ra-nextquarter01"))
    notice_service.recheck(store, _second_run(run, run_id="ra-nextquarter02"))  # 又坏了
    rows = notice_service.list_notices(store)["rows"]
    mine = [row for row in rows if row["key"] == key]
    assert len(mine) == 1  # 同一地址只保留一个通知编号
    assert mine[0]["assessment"] == "still_failing"
    assert mine[0]["status"] == "open"
    assert mine[0]["revisions"] == 2


def test_ledger_counts_by_outcome(settings, store, run, issued):
    defect_keys = [row["key"] for row in run["probes"] if row["category"] != "OK"]
    notice_service.recheck(store, _second_run(run, drop=set(defect_keys[:2]),
                                              recover=set(defect_keys[2:4])))
    ledger = notice_service.ledger(store)
    assert ledger["by_outcome"]["source_removed"] == 2
    assert ledger["by_outcome"]["recovered"] == 2
    assert ledger["by_outcome"]["still_failing"] == FINDINGS - 4
    assert ledger["open"] == FINDINGS - 2  # 来源移除仍在办
    assert ledger["recovered"] == 2


# -- 巡检完成后的自动复核 -----------------------------------------------------

def _serve_dir(root: pathlib.Path) -> str:
    spec = importlib.util.spec_from_file_location("gb632_sample_site_alt",
                                                  ROOT / "sample_site" / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    httpd = module.serve(str(root), 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def test_new_run_auto_rechecks_notices(settings, store, run, issued, tmp_path):
    """下一季度巡检完成即自动复核：移出的地址记来源移除，仍坏的记仍有故障。"""
    altered = tmp_path / "site_v2"
    shutil.copytree(ROOT / "sample_site", altered)
    catalog = altered / "site-a" / "catalog.html"
    text = catalog.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "report.pdf" in line)
    catalog.write_text(text.replace(line + "\n", ""), encoding="utf-8")

    from app import config
    settings_v2 = config.Settings(db_path=settings.db_path,
                                  sample_site_base=_serve_dir(altered),
                                  sample_site_dir=str(altered),
                                  eval_date=settings.eval_date)
    second = batch_service.run(settings_v2, store)["run"]
    assert second["run_id"] != run["run_id"]

    rows = {row["url"]: row for row in notice_service.list_notices(store)["rows"]}
    removed = rows[REMOVED_URL]
    assert removed["assessment"] == "source_removed"
    assert removed["status"] == "open"  # 移出来源不算已修复
    still = [row for url, row in rows.items()
             if url != REMOVED_URL and row["assessment"] == "still_failing"]
    assert len(still) == FINDINGS - 1
    assert len(store.list_notice_revisions()) == FINDINGS

    # 本次巡检按当前在册范围计缺陷：移出的地址不再出现在缺陷单里
    assert all(row["url"] != REMOVED_URL for row in second["defects"])
    assert second["totals"]["findings_total"] == FINDINGS - 1

    # 同输入重跑复用旧批次，不重复追加修订记录
    again = batch_service.run(settings_v2, store)
    assert again["reused"] is True
    assert len(store.list_notice_revisions()) == FINDINGS


# -- API、页面与导出 ----------------------------------------------------------

def test_notice_api_flow(client_with_run):
    issued = client_with_run.post("/api/notices/issue", json={}).json()
    assert issued["created"] == FINDINGS
    retry = client_with_run.post("/api/notices/issue", json={}).json()
    assert retry["created"] == 0 and retry["reused"] == FINDINGS

    body = client_with_run.get("/api/notices").json()
    assert body["count"] == FINDINGS
    assert body["rows"][0]["assessment"] == "pending"
    assert body["rows"][0]["assessment_label"] == "待复核"
    assert body["ledger"]["open"] == FINDINGS

    notice_no = body["rows"][0]["notice_no"]
    detail = client_with_run.get(f"/api/notices/{notice_no}").json()
    assert detail["basis"]["run_id"] == body["rows"][0]["opened_run_id"]
    assert detail["revision_rows"] == []

    missing = client_with_run.get("/api/notices/ZG-9999")
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_NO_NOTICE"

    bad = client_with_run.get("/api/notices?assessment=nope")
    assert bad.status_code == 400


def test_notice_csv_marks_outcomes(client_with_run):
    client_with_run.post("/api/notices/issue", json={})
    response = client_with_run.get("/api/notices.csv")
    assert response.status_code == 200
    text = response.text.lstrip("﻿")
    header = text.splitlines()[0]
    assert header.split(",")[:5] == ["通知编号", "站点", "分类", "地址", "复核结论"]
    assert len(text.splitlines()) == 1 + FINDINGS
    assert "待复核" in text
    assert "在办" in text


def test_notice_page_and_marks(client_with_run):
    client_with_run.post("/api/notices/issue", json={})
    page = client_with_run.get("/notices")
    assert page.status_code == 200
    assert "ZG-0001" in page.text
    assert "待复核" in page.text

    reconcile = client_with_run.get("/reconcile")
    assert "整改通知台账" in reconcile.text
    payload = client_with_run.get("/api/reconcile").json()
    assert payload["notice_ledger"]["open"] == FINDINGS
    assert payload["notice_ledger"]["by_outcome"]["pending"] == FINDINGS

    defects = client_with_run.get("/defects")
    assert "ZG-0001" in defects.text  # 缺陷单上标出已有通知的地址

    detail_page = client_with_run.get("/notices?notice_no=ZG-0001")
    assert "通知原始依据" in detail_page.text
    assert "修订记录" in detail_page.text


def test_notice_labels_registered():
    for outcome in tracking.OUTCOMES:
        assert reasons.notice_outcome_label(outcome) != outcome
    assert reasons.notice_outcome_label("source_removed") == "来源移除"
    assert reasons.notice_outcome_label("recovered") == "实测恢复"
    assert reasons.notice_outcome_label("still_failing") == "仍有故障"
