"""分层聚合明细数据集接口：另一个入口读取随下钻状态同步的聚合结果，并支持幂等回填。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drilldownagg import DrilldownAggService

router = APIRouter(prefix="/api/drilldownagg", tags=["分层聚合明细"])

service = DrilldownAggService()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="已启用、已归档、已替换"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分层聚合明细列表：历史数据集保持可读取；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """回填批次检查点：已完成、失败、写入中的批次都可核对。"""
    return {"items": service.list_batches()}


@router.post("/backfill", response_model=ActionResult)
def backfill(payload: EntryPayload) -> ActionResult:
    """提交回填批次：同一批次只写入一次；失败或中断后可重试，从上一完整批次继续。"""
    batch, message = service.backfill(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)
