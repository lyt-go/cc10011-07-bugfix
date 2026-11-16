"""多维下钻接口：维护下钻路径，覆盖提交路径评审、启用路径、归档路径等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drilldown import DrilldownService

router = APIRouter(prefix="/api/drilldown", tags=["多维下钻"])

service = DrilldownService()

LIST_FIELDS = ["路径编号", "起始维度", "下钻层级序列", "每层聚合粒度", "默认度量", "路径深度", "缓存秒数", "数据新鲜度"]
STATUSES = ["草稿", "路径评审中", "已启用", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="草稿、路径评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drilldown", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/aggregation", response_model=dict)
def get_aggregation(entry_id: int) -> dict:
    """分层聚合明细：按层级关联标准维度口径，逐层给出新鲜度与来源批次。

    没有可用路径快照时返回空态说明（layers 为空 + 说明），不报错。
    """
    detail = service.aggregation_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条下钻路径：完整保存层级及每层配置，校验不通过时说明原因。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="下钻路径已登记，层级与配置已完整保存", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条下钻路径执行提交路径评审、启用路径、归档路径。

    状态只能按评审、启用、归档推进；并发启用只保留一个路径快照，
    另一个请求返回 409 冲突。
    """
    action = str(payload.values.get("action") or "").strip()
    expected_raw = payload.values.get("expected_version")
    try:
        expected_version = int(expected_raw) if expected_raw is not None and str(expected_raw).strip() != "" else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_version 需为整数") from None
    entry, message, conflict = service.run_action(entry_id, action, expected_version=expected_version)
    if entry is None:
        if conflict:
            raise HTTPException(status_code=409, detail=message)
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/convert", response_model=ActionResult)
def convert_legacy(entry_id: int) -> ActionResult:
    """旧路径兼容转换：与新建路径共用同一套校验，转换失败说明原因并记录异常。"""
    entry, message = service.convert_legacy(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
