"""分层聚合明细数据集：随下钻路径状态同步生成/更新，支持幂等回填与断点续写。

数据来源与口径：
- 启用快照（层级明细 + 维度口径 + 映射版本）由下钻服务在启用/兼容转换时写入；
- 本模块按快照逐层生成聚合明细行，每层都带数据新鲜度、快照版本、映射版本，
  保证各层数据新鲜度与结果来源可追溯；
- 路径归档时明细行同步标记归档，历史数据保持可读取；
- 回填批次按批次号幂等（同一批次只写入一次），失败/中断后清理残留，
  从上一完整批次继续，重试不丢已完成的完整批次。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "drilldownagg"
BATCH_MODULE = "drilldownagg_batch"
RECORD_MODULE = "drilldownagg_record"
LOG_MODULE = "drilldown_log"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(entry: dict[str, Any], event: str, reason: str) -> None:
    """记录链路异常：启用失败、回填失败、中断续写等都留痕，便于排查与重试。"""
    rows = store.rows(LOG_MODULE)
    rows.append({
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "时间": _now(),
        "路径id": entry.get("id"),
        "路径编号": entry.get("路径编号"),
        "事件": event,
        "原因": reason,
    })


def _layer_metric_totals(path_id: int) -> dict[int, float]:
    """按层级汇总已完成的回填记录；只有完整批次的记录参与聚合。"""
    totals: dict[int, float] = {}
    for record in store.rows(RECORD_MODULE):
        if int(record.get("路径id", 0)) != path_id:
            continue
        layer_no = int(record.get("层级序号", 0))
        totals[layer_no] = totals.get(layer_no, 0.0) + float(record.get("度量值", 0))
    return totals


def _last_complete_batch(path_id: int) -> str:
    batch_no = ""
    finished_at = ""
    for batch in store.rows(BATCH_MODULE):
        if int(batch.get("路径id", 0)) != path_id or batch.get("状态") != "已完成":
            continue
        if str(batch.get("完成时间", "")) >= finished_at:
            finished_at = str(batch.get("完成时间", ""))
            batch_no = str(batch.get("批次号", ""))
    return batch_no


def _refresh_metrics(path_id: int) -> None:
    """回填完成后刷新当前启用明细行的度量值与来源批次，两个入口看到同一份结果。"""
    totals = _layer_metric_totals(path_id)
    batch_no = _last_complete_batch(path_id)
    for row in store.rows(MODULE):
        if int(row.get("路径id", 0)) == path_id and row.get("状态") == "已启用":
            row["度量值"] = totals.get(int(row.get("层级序号", 0)), 0)
            row["来源批次"] = batch_no


def _discard_batch_records(batch_no: str) -> int:
    """清理未完整写入批次的残留记录，保证中断后从上一完整批次继续。"""
    rows = store.rows(RECORD_MODULE)
    kept = [record for record in rows if str(record.get("批次号")) != batch_no]
    removed = len(rows) - len(kept)
    if removed:
        rows[:] = kept
    return removed


def sync_for_path(entry: dict[str, Any], snapshot: dict[str, Any] | None, action: str) -> None:
    """分层聚合结果随下钻状态同步更新。

    启用/兼容转换：作废旧启用行（保留历史可读），按新快照逐层写入明细行；
    归档：当前启用行标记归档，历史数据保持可读取。
    """
    rows = store.rows(MODULE)
    path_id = int(entry.get("id", 0))
    if action == "归档":
        for row in rows:
            if int(row.get("路径id", 0)) == path_id and row.get("状态") == "已启用":
                row["状态"] = "已归档"
                row["归档时间"] = _now()
        return
    if snapshot is None:
        return
    for row in rows:
        if int(row.get("路径id", 0)) == path_id and row.get("状态") == "已启用":
            row["状态"] = "已替换"  # 被新快照替换，历史仍可读
    totals = _layer_metric_totals(path_id)
    batch_no = _last_complete_batch(path_id)
    caliber = snapshot.get("维度口径") or []
    next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    for index, layer in enumerate(snapshot.get("层级明细") or []):
        standard = ""
        if index < len(caliber):
            standard = str(caliber[index].get("标准维度") or "")
        rows.append({
            "id": next_id,
            "路径id": path_id,
            "路径编号": entry.get("路径编号"),
            "层级序号": index + 1,
            "维度": layer.get("维度"),
            "标准维度": standard,
            "聚合粒度": layer.get("聚合粒度"),
            "默认度量": layer.get("默认度量"),
            "缓存秒数": layer.get("缓存秒数"),
            "数据新鲜度": layer.get("数据新鲜度"),
            "度量值": totals.get(index + 1, 0),
            "来源批次": batch_no,
            "快照版本": snapshot.get("快照版本"),
            "映射版本": snapshot.get("映射版本"),
            "状态": "已启用",
            "生成时间": _now(),
        })
        next_id += 1


class DrilldownAggService:
    """分层聚合明细数据集的读取与回填。"""

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: (str(row.get("路径编号", "")), int(row.get("快照版本", 0)), int(row.get("层级序号", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_batches(self) -> list[dict[str, Any]]:
        return list(store.rows(BATCH_MODULE))

    def rows_for_path(self, path_id: int, snapshot_version: int | None = None) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(MODULE) if int(row.get("路径id", 0)) == path_id]
        if snapshot_version is not None:
            rows = [row for row in rows if int(row.get("快照版本", 0)) == snapshot_version]
        rows.sort(key=lambda row: int(row.get("层级序号", 0)))
        return rows

    def backfill(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """写入一个回填批次。

        - 同一批次号重复提交：已完成则跳过，只写入一次；
        - 上次写入失败/中断：清理残留记录，从上一完整批次继续，允许重试；
        - 新记录按当前规则写入（记录当前路径深度校验），历史批次保持可读。
        """
        batch_no = str(values.get("批次号") or "").strip()
        if not batch_no:
            return None, "缺少必填字段：批次号"
        entry = None
        path_id = values.get("路径id")
        path_code = str(values.get("路径编号") or "").strip()
        for row in store.rows("drilldown"):
            if path_id is not None and int(row.get("id", 0)) == int(path_id):
                entry = row
                break
            if path_code and str(row.get("路径编号")) == path_code:
                entry = row
                break
        if entry is None:
            return None, f"下钻路径不存在（路径id={path_id or '空'}，路径编号={path_code or '空'}），批次 {batch_no} 无法写入"
        records = values.get("记录")
        if not isinstance(records, list) or not records:
            return None, "批次记录为空，请提供至少一条记录（层级序号、维度取值、度量值）"
        with _lock:
            batches = store.rows(BATCH_MODULE)
            batch = next((item for item in batches if str(item.get("批次号")) == batch_no), None)
            if batch is not None and batch.get("状态") == "已完成":
                return batch, f"批次 {batch_no} 已写入过，本次跳过（同一批次只写入一次）"
            if batch is not None:
                removed = _discard_batch_records(batch_no)
                batch["重试次数"] = int(batch.get("重试次数", 0)) + 1
                if removed:
                    log_event(entry, "回填中断续写", f"批次 {batch_no} 清理 {removed} 条残留记录，从上一完整批次继续")
            else:
                batch = {
                    "id": max((int(item.get("id", 0)) for item in batches), default=0) + 1,
                    "批次号": batch_no,
                    "路径id": int(entry["id"]),
                    "路径编号": entry.get("路径编号"),
                    "重试次数": 0,
                }
                batches.append(batch)
            batch.update({"状态": "写入中", "异常原因": "", "开始时间": _now()})
            try:
                depth = int(entry.get("路径深度") or len(entry.get("层级明细") or []) or 0)
                prepared = []
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        raise ValueError(f"第 {index + 1} 条记录格式不正确")
                    layer_no = int(record.get("层级序号"))
                    if depth and (layer_no < 1 or layer_no > depth):
                        raise ValueError(f"第 {index + 1} 条记录层级序号 {layer_no} 超出路径深度 {depth}")
                    metric = float(record.get("度量值"))
                    prepared.append({
                        "批次号": batch_no,
                        "路径id": int(entry["id"]),
                        "层级序号": layer_no,
                        "维度取值": str(record.get("维度取值") or ""),
                        "度量值": metric,
                    })
                store.rows(RECORD_MODULE).extend(prepared)
                batch["状态"] = "已完成"
                batch["记录数"] = len(prepared)
                batch["完成时间"] = _now()
            except (TypeError, ValueError) as exc:
                _discard_batch_records(batch_no)
                batch["状态"] = "失败"
                batch["异常原因"] = str(exc)
                entry["abnormal"] = True
                entry["最近异常"] = f"批次 {batch_no} 写入失败：{exc}"
                log_event(entry, "回填批次失败", str(exc))
                return None, f"批次 {batch_no} 写入失败：{exc}；已回退到上一完整批次，可修正后重试"
            _refresh_metrics(int(entry["id"]))
            entry["abnormal"] = False
            entry.pop("最近异常", None)
            return batch, f"批次 {batch_no} 已写入 {len(prepared)} 条记录"
