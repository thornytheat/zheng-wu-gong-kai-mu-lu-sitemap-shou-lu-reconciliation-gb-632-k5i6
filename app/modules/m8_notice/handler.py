"""M8 入口：整改通知页面、出单/复核 API、台账 CSV 导出。

出单与复核都是显式动作 + 巡检完成后的自动复核（见 m5_batches/service.py），
两条路径走同一个 service，结论一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ... import errors
from ...web import Ctx, get_ctx, render
from . import csv_out, service

router = APIRouter(tags=["M8 整改通知"])


@router.post("/api/notices/issue")
def api_issue(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return service.issue(ctx.store, run_id=body.get("run_id"),
                         site=body.get("site") or None,
                         category=body.get("category") or None)


@router.post("/api/notices/recheck")
def api_recheck(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    run = ctx.current_run(body.get("run_id"))
    if run is None:
        raise errors.NotFound("还没有巡检批次，先跑一次巡检", code="E_NO_RUN")
    return service.recheck(ctx.store, run)


@router.get("/api/notices")
def api_notices(site: str | None = None, assessment: str | None = None,
                status: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.list_notices(ctx.store, site, assessment, status)


@router.get("/api/notices.csv")
def api_notices_csv(site: str | None = None, assessment: str | None = None,
                    status: str | None = None,
                    ctx: Ctx = Depends(get_ctx)) -> Response:
    rows = service.list_notices(ctx.store, site, assessment, status)["rows"]
    return Response(content=csv_out.notices_csv(rows),
                    media_type="text/csv; charset=utf-8",
                    headers={"content-disposition":
                             f'attachment; filename="{csv_out.filename()}"'})


@router.get("/api/notices/{notice_no}")
def api_notice_detail(notice_no: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.detail(ctx.store, notice_no)


@router.get("/notices")
def page_notices(request: Request, site: str | None = None,
                 assessment: str | None = None, status: str | None = None,
                 notice_no: str | None = None):
    ctx: Ctx = get_ctx(request)
    data = service.list_notices(ctx.store, site, assessment, status)
    chosen = service.detail(ctx.store, notice_no) if notice_no else None
    return render(request, "notices.html", active="notice", data=data,
                  rows=data["rows"], ledger=data["ledger"],
                  outcomes=data["outcomes"], statuses=data["statuses"],
                  site=site or "", assessment=assessment or "",
                  status=status or "", chosen=chosen)
