"""旧版审计文本 ⇄ 版本化字段差异。

历史审计记录只有一个「变更内容」自由文本字段，新页面需要逐字段的
before / after。解析器不做猜测性改写：能识别结构化写法就拆成字段差异，
识别不了就退化成单字段差异，原文始终由调用方另行保留。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 兼容不同批次可能提交的字段别名，统一输出 field / before / after。
FIELD_KEYS = ("field", "字段", "字段名", "name", "名称")
BEFORE_KEYS = ("before", "old", "old_value", "旧值", "原值", "变更前")
AFTER_KEYS = ("after", "new", "new_value", "新值", "变更后")
SEPARATORS = re.compile(r"[;；\n]+")
ARROW = re.compile(r"\s*(?:->|→|=>|＞|⇒)\s*")
CHANGE_WORD = re.compile(r"\s*(?:变更为|改为|更新为|调整为|修改为)\s*")
FROM_WORD = re.compile(r"\s*(?:从|由)\s*")
COLON = re.compile(r"\s*[:：]\s*")

FALLBACK_FIELD = "变更内容"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def normalize_changes(value: Any) -> list[dict[str, str]]:
    """把接口 / 归档里的差异结构统一成 field、before、after 三个字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        return parse_legacy_text(value)
    if isinstance(value, dict):
        # 既支持 {"字段": {"before": ..., "after": ...}}，也支持单条差异对象。
        if _pick(value, FIELD_KEYS) is not None:
            items: list[Any] = [value]
        else:
            items = [
                {"field": key, "before": val.get("before") if isinstance(val, dict) else "",
                 "after": val.get("after") if isinstance(val, dict) else val}
                for key, val in value.items()
            ]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]

    changes: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            field = _text(_pick(item, FIELD_KEYS))
            before = _text(_pick(item, BEFORE_KEYS))
            after = _text(_pick(item, AFTER_KEYS))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            field, before, after = (_text(item[0]), _text(item[1]), _text(item[2]))
        else:
            field, before, after = FALLBACK_FIELD, "", _text(item)
        if not field:
            field = FALLBACK_FIELD
        changes.append({"field": field, "before": before, "after": after})
    return changes


def _split_pair(segment: str) -> tuple[str, str, str] | None:
    """解析单个片段，返回 (field, before, after)；无法识别返回 None。"""
    # 1) 字段: 旧值 -> 新值
    if COLON.search(segment):
        field, rest = COLON.split(segment, maxsplit=1)
        field = field.strip()
        rest = rest.strip()
        if field and rest:
            if ARROW.search(rest):
                before, after = ARROW.split(rest, maxsplit=1)
                return field, before.strip(), after.strip()
            if CHANGE_WORD.search(rest):
                before, after = CHANGE_WORD.split(rest, maxsplit=1)
                if FROM_WORD.search(before):
                    before = FROM_WORD.split(before, maxsplit=1)[-1]
                return field, before.strip(), after.strip()
            # 只有「字段: 新值」时把旧值留空，仍能给出字段级差异。
            return field, "", rest
    # 2) 字段由旧值变更为新值 / 字段从旧值改为新值
    if CHANGE_WORD.search(segment):
        field_part, after = CHANGE_WORD.split(segment, maxsplit=1)
        if FROM_WORD.search(field_part):
            field, before = FROM_WORD.split(field_part, maxsplit=1)
            return field.strip(), before.strip(), after.strip()
    # 3) 没有字段名时，整段作为单字段差异
    return None


def parse_legacy_text(text: Any) -> list[dict[str, str]]:
    """将一段旧版自由文本解析成字段差异。

    支持的写法（可混排、用 ; ； 或换行分隔）：
    - ``指标名称: 旧值 -> 新值``
    - ``指标名称：旧值→新值``
    - ``指标名称由旧值变更为新值``
    - ``指标名称 从 旧值 改为 新值``
    - JSON 对象 / 数组（字段别名同上）
    """
    raw = _text(text)
    if not raw:
        return []

    stripped = raw.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            changes = normalize_changes(parsed)
            if changes:
                return changes

    segments = [segment.strip() for segment in SEPARATORS.split(raw) if segment.strip()]
    parsed_segments: list[dict[str, str]] = []
    for segment in segments:
        pair = _split_pair(segment)
        if pair is None:
            parsed_segments = []
            break
        field, before, after = pair
        parsed_segments.append({"field": field, "before": before, "after": after})

    if parsed_segments:
        return parsed_segments
    return [{"field": FALLBACK_FIELD, "before": "", "after": raw}]


def diff_summary(changes: list[dict[str, str]]) -> str:
    if not changes:
        return ""
    return "；".join(
        f"{item.get('field', '')}: {item.get('before', '') or '∅'} -> {item.get('after', '')}"
        for item in changes
    )


def diff_field_names(changes: list[dict[str, str]]) -> list[str]:
    return [str(item.get("field", "")) for item in changes if item.get("field")]
