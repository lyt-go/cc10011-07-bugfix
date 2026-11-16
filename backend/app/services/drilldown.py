"""多维下钻业务规则：状态流转、字段校验、并发启用与分层聚合联动都收在这里。

关键约定：
- 新建即完整保存层级明细与每层配置（聚合粒度/默认度量/缓存秒数/数据新鲜度），
  层级顺序按用户提交顺序保存，不做任何排序；
- 状态只能按 草稿 -> 路径评审中 -> 已启用 -> 已归档 推进，已归档为终态；
- 启用时校验层级重复、跨层粒度递进、起始维度一致，并冻结一份路径快照
  （层级明细 + 维度口径 + 映射版本），并发启用只保留一个快照，另一个返回冲突；
- 旧路径兼容转换与新建路径共用 app.services.drilldown_path 里的同一套校验；
- 启用/归档会同步刷新分层聚合明细数据集，聚合结果与启用路径引用同一维度口径。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services import dimensionmap as dimensionmap_service
from app.services import drilldownagg as drilldownagg_service
from app.services.drilldown_path import build_path_config, normalize_layers, validate_layers
from app.store import store

MODULE = "drilldown"
SNAPSHOT_MODULE = "drilldown_snapshot"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级序列"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已归档"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "归档路径": "已归档"}
# 状态只能按 评审 -> 启用 -> 归档 推进；已归档是终态，不能再启用
ALLOWED_TRANSITIONS = {
    "提交路径评审": {"草稿"},
    "启用路径": {"路径评审中"},
    "归档路径": {"草稿", "路径评审中", "已启用"},
}

# 启用互斥锁：并发启用同一路径时串行化，后到请求在锁内看到已启用状态后返回冲突
_enable_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DrilldownService:
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
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        config, errors = build_path_config(values)
        if errors:
            return None, errors
        rows = store.rows(MODULE)
        path_code = str(values.get("路径编号")).strip()
        if any(row.get("路径编号") == path_code and row.get("status") != "已归档" for row in rows):
            return None, [f"路径编号 {path_code} 已存在且未归档，不能重复登记"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1, "路径编号": path_code}
        entry.update(config or {})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["版本"] = 1
        entry["创建时间"] = _now()
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """执行状态动作。返回 (记录, 说明, 是否并发冲突)。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围", False
        if action == "启用路径":
            return self._enable(entry, expected_version)
        allowed = ALLOWED_TRANSITIONS[action]
        current = str(entry.get("status", ""))
        if current not in allowed:
            if current == "已归档":
                return None, f"路径 {entry.get('路径编号')} 已归档，归档是终态，不能再执行「{action}」", False
            return None, f"当前状态「{current}」不允许执行「{action}」，状态只能按评审、启用、归档推进", False
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        if target == "已归档":
            drilldownagg_service.sync_for_path(entry, None, "归档")
        return entry, f"下钻路径已{action}", False

    def _enable(
        self,
        entry: dict[str, Any],
        expected_version: int | None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        with _enable_lock:
            current = str(entry.get("status", ""))
            if current == "已启用":
                return None, f"路径 {entry.get('路径编号')} 已被另一请求启用（当前快照 v{entry.get('当前快照版本', '-')}），本次启用冲突", True
            if current == "已归档":
                return None, f"路径 {entry.get('路径编号')} 已归档，归档路径不能再启用", False
            if current != "路径评审中":
                return None, f"当前状态「{current}」不允许启用，需先提交路径评审", False
            if expected_version is not None and int(expected_version) != int(entry.get("版本", 1)):
                return None, f"路径版本已变化（当前 v{entry.get('版本')}），请刷新后重试", True
            layers = entry.get("层级明细") or normalize_layers(entry)
            errors = validate_layers(layers, entry.get("起始维度"))
            if errors:
                reason = "；".join(errors)
                entry["abnormal"] = True
                entry["最近异常"] = reason
                drilldownagg_service.log_event(entry, "启用路径失败", reason)
                return None, f"启用校验未通过：{reason}", False
            snapshot = self._create_snapshot(entry, layers)
            entry["status"] = "已启用"
            entry["pending"] = True
            entry["abnormal"] = False
            entry.pop("最近异常", None)
            drilldownagg_service.sync_for_path(entry, snapshot, "启用")
            unmapped = [item["维度"] for item in snapshot["维度口径"] if item["口径状态"] == "未映射"]
            message = f"下钻路径已启用（快照 v{snapshot['快照版本']}，映射版本 v{snapshot['映射版本']}）"
            if unmapped:
                message += f"；注意：{'、'.join(unmapped)} 未映射标准维度，聚合结果按原维度口径记录"
            return entry, message, False

    def _create_snapshot(self, entry: dict[str, Any], layers: list[dict[str, str]]) -> dict[str, Any]:
        """冻结启用快照：层级明细 + 维度口径 + 映射版本，聚合结果与启用路径引用同一口径。"""
        mappings, mapping_version = dimensionmap_service.active_standard_mappings()
        entry["版本"] = int(entry.get("版本", 1)) + 1
        caliber = []
        for layer in layers:
            standard = mappings.get(layer["维度"])
            caliber.append({
                "维度": layer["维度"],
                "标准维度": standard or "",
                "口径状态": "已映射" if standard else "未映射",
            })
        snapshots = store.rows(SNAPSHOT_MODULE)
        snapshot = {
            "id": max((int(row.get("id", 0)) for row in snapshots), default=0) + 1,
            "路径id": entry["id"],
            "路径编号": entry.get("路径编号"),
            "快照版本": entry["版本"],
            "层级明细": [dict(layer) for layer in layers],
            "维度口径": caliber,
            "映射版本": mapping_version,
            "启用时间": _now(),
        }
        snapshots.append(snapshot)
        entry["当前快照版本"] = entry["版本"]
        entry["层级明细"] = [dict(layer) for layer in layers]
        return snapshot

    def convert_legacy(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """旧路径兼容转换：与新建路径共用同一套归一化与校验实现。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if entry.get("层级明细"):
            return None, "该路径已是新版层级结构，无需转换"
        config, errors = build_path_config(entry)
        if errors:
            reason = "；".join(errors)
            entry["abnormal"] = True
            entry["最近异常"] = reason
            drilldownagg_service.log_event(entry, "兼容转换失败", reason)
            return None, f"兼容转换未通过：{reason}"
        entry.update(config or {})
        entry["转换时间"] = _now()
        entry["abnormal"] = False
        entry.pop("最近异常", None)
        message = "旧路径已完成兼容转换，层级与配置按同一套校验规则保存"
        if entry.get("status") == "已启用":
            snapshot = self._create_snapshot(entry, list(entry["层级明细"]))
            drilldownagg_service.sync_for_path(entry, snapshot, "启用")
            message += f"，并补齐启用快照 v{snapshot['快照版本']}，分层聚合结果已同步"
        return entry, message

    def aggregation_detail(self, entry_id: int) -> dict[str, Any] | None:
        """分层聚合明细：按层级关联标准维度口径，逐层给出新鲜度与来源，可追溯。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        _, current_mapping_version = dimensionmap_service.active_standard_mappings()
        snapshots = [row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("路径id", 0)) == entry_id]
        base: dict[str, Any] = {
            "路径编号": entry.get("路径编号"),
            "状态": entry.get("status"),
            "当前映射版本": current_mapping_version,
        }
        if not snapshots:
            base.update({
                "快照版本": None,
                "映射版本": None,
                "口径一致": None,
                "layers": [],
                "说明": "暂无结果：路径尚未启用或未完成兼容转换，没有可追溯的分层聚合数据",
            })
            return base
        snapshot = max(snapshots, key=lambda row: int(row.get("快照版本", 0)))
        snapshot_version = int(snapshot.get("快照版本", 0))
        rows = drilldownagg_service.DrilldownAggService().rows_for_path(entry_id, snapshot_version)
        layers = []
        for row in rows:
            layers.append({
                "层级序号": row.get("层级序号"),
                "维度": row.get("维度"),
                "标准维度": row.get("标准维度"),
                "聚合粒度": row.get("聚合粒度"),
                "默认度量": row.get("默认度量"),
                "缓存秒数": row.get("缓存秒数"),
                "数据新鲜度": row.get("数据新鲜度"),
                "度量值": row.get("度量值"),
                "来源批次": row.get("来源批次"),
                "快照版本": row.get("快照版本"),
                "映射版本": row.get("映射版本"),
                "数据状态": row.get("状态"),
                "生成时间": row.get("生成时间"),
                "口径一致": int(row.get("映射版本", 0)) == current_mapping_version,
            })
        base.update({
            "快照版本": snapshot_version,
            "映射版本": snapshot.get("映射版本"),
            "口径一致": int(snapshot.get("映射版本", 0)) == current_mapping_version,
            "layers": layers,
            "说明": "" if layers else "暂无结果：分层聚合数据尚未生成，可先提交回填批次",
        })
        return base
