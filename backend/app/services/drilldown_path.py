"""下钻路径的归一化与校验：新建路径与旧路径兼容转换共用同一套实现。

新旧两种输入都先归一化成有序层级明细，再走同一份校验规则，
避免旧路径兼容转换另写一遍校验导致口径漂移。
"""
from __future__ import annotations

from typing import Any

# 聚合粒度由粗到细：下钻要求逐层变细，序号必须严格递增
GRANULARITY_RANK = {"年": 0, "季": 1, "月": 2, "周": 3, "天": 4, "小时": 5, "分钟": 6, "秒": 7}

LAYER_FIELDS = ["维度", "聚合粒度", "默认度量", "缓存秒数", "数据新鲜度"]

# 旧格式层级序列/平行字段支持的分隔符，按优先级逐个尝试
SEPARATORS = [">", "/", "|", "，", ",", "、"]


def _split_sequence(raw: Any) -> list[str]:
    """把层级序列拆成有序列表；列表输入原样保留顺序，不做任何排序。"""
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    for sep in SEPARATORS:
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _split_parallel(raw: Any, count: int) -> list[str]:
    """旧格式里与层级平行的字段（每层聚合粒度等）：分隔串按层对齐，单值广播到每层。"""
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw]
    else:
        text = str(raw or "").strip()
        values = []
        if text:
            for sep in SEPARATORS:
                if sep in text:
                    values = [part.strip() for part in text.split(sep)]
                    break
            else:
                values = [text]
    if not values:
        return [""] * count
    if len(values) == 1 and count > 1:
        return values * count
    values = values[:count]
    return values + [""] * (count - len(values))


def normalize_layers(values: dict[str, Any]) -> list[dict[str, str]]:
    """把新旧两种输入统一成有序层级明细列表。

    新格式：下钻层级序列是 [{"维度": ..., "聚合粒度": ..., ...}, ...]，顺序即下钻顺序。
    旧格式：下钻层级序列是 "维度A>维度B" 字符串，每层聚合粒度/默认度量/缓存秒数/
    数据新鲜度是与之平行的字段（分隔串或单值），按位置对齐到每一层。
    """
    raw = values.get("下钻层级序列")
    if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], dict):
        layers = []
        for item in raw:
            layer = {field: str((item or {}).get(field) or "").strip() for field in LAYER_FIELDS}
            if layer["维度"]:
                layers.append(layer)
        return layers
    names = _split_sequence(raw)
    granularity = _split_parallel(values.get("每层聚合粒度"), len(names))
    measure = _split_parallel(values.get("默认度量"), len(names))
    cache = _split_parallel(values.get("缓存秒数"), len(names))
    freshness = _split_parallel(values.get("数据新鲜度"), len(names))
    return [
        {
            "维度": name,
            "聚合粒度": granularity[index],
            "默认度量": measure[index],
            "缓存秒数": cache[index],
            "数据新鲜度": freshness[index],
        }
        for index, name in enumerate(names)
    ]


def validate_layers(layers: list[dict[str, str]], start_dimension: Any) -> list[str]:
    """同一套校验：层级重复、跨层粒度递进、起始维度一致。新建与旧路径转换共用。"""
    errors: list[str] = []
    if not layers:
        return ["下钻层级序列为空，至少需要一个层级"]
    seen: set[str] = set()
    duplicates: list[str] = []
    for layer in layers:
        dimension = layer["维度"]
        if dimension in seen and dimension not in duplicates:
            duplicates.append(dimension)
        seen.add(dimension)
    if duplicates:
        errors.append(f"层级维度重复：{'、'.join(duplicates)}")
    previous_rank: int | None = None
    previous_dimension = ""
    for layer in layers:
        granularity = layer["聚合粒度"]
        rank = GRANULARITY_RANK.get(granularity)
        if rank is None:
            errors.append(f"层级「{layer['维度']}」的聚合粒度「{granularity or '空'}」无法识别")
        elif previous_rank is not None and rank <= previous_rank:
            errors.append(
                f"层级「{layer['维度']}」的聚合粒度「{granularity}」必须比上一层「{previous_dimension}」更细"
            )
        if rank is not None:
            previous_rank = rank
        previous_dimension = layer["维度"]
        cache_seconds = layer.get("缓存秒数", "")
        if cache_seconds and not cache_seconds.isdigit():
            errors.append(f"层级「{layer['维度']}」的缓存秒数「{cache_seconds}」需为非负整数")
    start = str(start_dimension or "").strip()
    if start and layers[0]["维度"] != start:
        errors.append(f"起始维度「{start}」与第一层维度「{layers[0]['维度']}」不一致")
    return errors


def build_path_config(values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """归一化 + 校验，产出可持久化的路径配置。

    新建路径、启用前校验、旧路径兼容转换都走这里，保证新旧路径同一套口径。
    返回 (配置, 错误列表)；有错误时配置为 None。
    """
    layers = normalize_layers(values)
    errors = validate_layers(layers, values.get("起始维度"))
    if errors:
        return None, errors
    config = {
        "起始维度": str(values.get("起始维度") or "").strip(),
        "下钻层级序列": ">".join(layer["维度"] for layer in layers),
        "每层聚合粒度": ">".join(layer["聚合粒度"] for layer in layers),
        "默认度量": ">".join(layer["默认度量"] for layer in layers),
        "缓存秒数": ">".join(layer["缓存秒数"] for layer in layers),
        "数据新鲜度": ">".join(layer["数据新鲜度"] for layer in layers),
        "路径深度": str(len(layers)),
        "层级明细": [dict(layer) for layer in layers],
    }
    return config, []
