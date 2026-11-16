"""维度映射业务规则：状态流转、字段校验与筛选口径都收在这里。

规则生效时顺着原调用链（路由 -> run_action）同步写入标准维度映射结果，
多维下钻的启用与分层聚合只读这份结果，保证上下游引用同一维度口径。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "dimensionmap"
STANDARD_MODULE = "dimensionmap_standard"
REQUIRED_FIELDS = ["规则编号", "原始维度名", "标准维度名"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

_standard_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_standard_seeded() -> None:
    """存量已生效规则首次访问时补写标准维度映射结果（幂等，只补一次）。"""
    if store.rows(STANDARD_MODULE):
        return
    with _standard_lock:
        rows = store.rows(STANDARD_MODULE)
        if rows:
            return
        version = 0
        for rule in store.rows(MODULE):
            if rule.get("status") != "已生效":
                continue
            version += 1
            rows.append({
                "id": version,
                "规则编号": rule.get("规则编号"),
                "原始维度名": rule.get("原始维度名"),
                "标准维度名": rule.get("标准维度名"),
                "映射版本": version,
                "状态": "已生效",
                "生效时间": rule.get("最近更新时间") or _now(),
                "来源": "存量规则初始化",
            })


def active_standard_mappings() -> tuple[dict[str, str], int]:
    """当前生效的标准维度映射：原始维度名 -> 标准维度名，以及最新映射版本号。"""
    _ensure_standard_seeded()
    mapping: dict[str, str] = {}
    version = 0
    for row in store.rows(STANDARD_MODULE):
        version = max(version, int(row.get("映射版本", 0)))
        if row.get("状态") == "已生效":
            mapping[str(row.get("原始维度名"))] = str(row.get("标准维度名"))
    return mapping, version


def sync_standard_mapping(entry: dict[str, Any]) -> dict[str, Any]:
    """规则生效时同步写入标准维度映射结果；同一原始维度只保留一个生效版本。"""
    _ensure_standard_seeded()
    with _standard_lock:
        rows = store.rows(STANDARD_MODULE)
        version = max((int(row.get("映射版本", 0)) for row in rows), default=0) + 1
        for row in rows:
            if row.get("原始维度名") == entry.get("原始维度名") and row.get("状态") == "已生效":
                row["状态"] = "已废弃"  # 旧版本作废但保留，历史口径可追溯
                row["被替代版本"] = version
        record = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "规则编号": entry.get("规则编号"),
            "原始维度名": entry.get("原始维度名"),
            "标准维度名": entry.get("标准维度名"),
            "映射版本": version,
            "状态": "已生效",
            "生效时间": _now(),
            "来源": "规则生效同步",
        }
        rows.append(record)
        return record


def deprecate_standard_mapping(entry: dict[str, Any]) -> None:
    """规则废弃时同步作废对应的标准维度映射结果（保留记录，历史可读）。"""
    _ensure_standard_seeded()
    with _standard_lock:
        for row in store.rows(STANDARD_MODULE):
            if row.get("原始维度名") == entry.get("原始维度名") and row.get("状态") == "已生效":
                row["状态"] = "已废弃"
                row["废弃时间"] = _now()


class DimensionmapService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_standard(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """标准维度映射结果：含历史版本，口径变化可核对。"""
        _ensure_standard_seeded()
        rows = list(store.rows(STANDARD_MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("原始维度名", "")) or keyword in str(row.get("标准维度名", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("映射类型", "值转换逻辑", "默认取值", "映射优先级"):
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry["最近更新时间"] = _now()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度映射可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["最近更新时间"] = _now()
        message = f"映射规则已{action}"
        if target == "已生效":
            record = sync_standard_mapping(entry)
            message += f"，标准维度映射结果已同步（映射版本 v{record['映射版本']}）"
        elif target == "已废弃":
            deprecate_standard_mapping(entry)
            message += "，对应标准维度映射结果已同步作废"
        return entry, message
