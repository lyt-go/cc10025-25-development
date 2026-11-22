"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, DispatchPayload, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/prefetch_capacity/summary")
def capacity_summary() -> dict[str, Any]:
    """按冷存对象聚合的计划量与对象去重物理量，供派发前核对实际读量。"""
    return require_service("prefetch_capacity").capacity_summary()


@router.get("/prefetch_capacity/overview")
def capacity_overview(as_of: str = Query(default="2026-11-22T13:00")) -> dict[str, Any]:
    """容量概览：各时间段实际读取、暂存占用，以及每个对象/目标的数据去向。"""
    return require_service("prefetch_capacity").overview(as_of)


@router.get("/prefetch_capacity/snapshot")
def capacity_snapshot(as_of: str = Query(default="2026-11-22T13:00")) -> dict[str, Any]:
    """按时间点核对：A/临时任务/B 实际取得的数据、暂存占用与是否按期交付。"""
    return require_service("prefetch_capacity").snapshot(as_of)


@router.get("/prefetch_capacity/tradeoff")
def capacity_tradeoff() -> dict[str, Any]:
    """复用暂存省读量与临时请求交付时限之间的冲突说明。"""
    return require_service("prefetch_capacity").tradeoff()


@router.post("/prefetch_capacity/dispatch")
def dispatch_capacity(payload: DispatchPayload) -> ActionResult:
    """冷存储管理读取容量明细后实际派发一次冷存读取。"""
    service = require_service("prefetch_capacity")
    entry, message = service.dispatch(
        payload.capacity_id, payload.as_of, hold_until=payload.hold_until)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/prefetch_capacity/{entry_id}/actions")
def run_capacity_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service("prefetch_capacity")
    action = payload.action.strip()
    if action == "派发读取":
        entry, message = service.dispatch(
            entry_id, payload.effective_at or payload.remark or "2026-11-22T13:00")
        return ActionResult(ok=entry is not None, message=message, entry=entry)
    if action == "回收占用":
        result = service.reclaim(payload.effective_at or payload.remark or "2026-11-22T23:59")
        return ActionResult(
            ok=bool(result.get("ok")), message=str(result.get("message", "")),
            entry={"reclaim": result})
    if action == "重置演示场景":
        result = service.reset_scenario()
        return ActionResult(ok=True, message=str(result["message"]),
                            entry={"reset": result})
    return ActionResult(ok=False, message=f"动作「{action}」不属于{service.ENTITY}可执行范围")


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
