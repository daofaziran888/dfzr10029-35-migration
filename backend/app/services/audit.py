"""数据审计业务规则：登记、状态流转、旧版文本迁移与版本化差异。

迁移约定（审计页与导出共用同一口径）：
- 「变更内容」永远保留旧版原文，作为来源依据；
- 「变更差异」是唯一可消费的字段级结构，新登记事件也直接写这个结构；
- 迁移批次记录归档输入（旧文本 + 操作人归档明细），失败整批回滚，
  已迁移记录仍可按批次回滚到旧版文本。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from app.services.audit_diff import (
    FALLBACK_FIELD,
    diff_summary,
    normalize_changes,
    parse_legacy_text,
)
from app.store import AuditMigrationError, store

MODULE = "audit"
BATCH_MODULE = "audit_migration_batch"
ARCHIVE_MODULE = "audit_operator_archive"

LIST_FIELDS = ["记录编号", "操作类型", "操作人", "操作对象", "操作时间", "变更内容", "审计结果", "记录状态"]
DIFF_FIELDS = ["内容版本", "变更差异", "差异摘要", "源文本归档", "迁移批次", "迁移时间"]
EXPORT_FIELDS = LIST_FIELDS + ["内容版本", "差异摘要", "迁移批次", "源文本归档"]
REQUIRED_FIELDS = ["记录编号", "操作类型", "操作人"]
STATUS_ORDER = ["已记录", "已审计", "异常"]
ACTION_RULES = {"提交审计": "已审计", "标记异常": "异常"}
NEGATIVE_ACTIONS = []

VERSION_LEGACY = "旧版文本"
VERSION_DIFF = "版本化差异"
BATCH_COMMITTED = "已迁移"
BATCH_ROLLED_BACK = "已回滚"
BATCH_FAILED = "迁移失败"
SCHEMA_VERSION = "audit-diff/v2"
CONFLICT_POLICIES = {"skip": "跳过并保留既有结果", "fail": "整批失败并回滚"}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_unstructured(changes: list[dict[str, str]]) -> bool:
    return not changes or any(item.get("field") == FALLBACK_FIELD for item in changes)


class AuditService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        batch_id: str | None = None,
        content_version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("记录编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if batch_id:
            rows = [row for row in rows if _text(row.get("迁移批次")) == batch_id.strip()]
        if content_version:
            rows = [row for row in rows if self._content_version(row) == content_version.strip()]
        total = len(rows)
        start = (page - 1) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._decorate(row) if row else None

    def _content_version(self, row: dict[str, Any]) -> str:
        return _text(row.get("内容版本")) or VERSION_LEGACY

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        changes = row.get("变更差异")
        # 只有迁移过（或登记时直接写入）的记录才暴露差异结构；
        # 旧版文本在迁移前只保留原文，避免查询混出两种格式。
        item["变更差异"] = [dict(change) for change in changes] if isinstance(changes, list) else []
        item["差异摘要"] = diff_summary(item["变更差异"])
        item["内容版本"] = self._content_version(row)
        item.setdefault("迁移批次", None)
        return item

    def _archive_rows(self) -> dict[int, dict[str, Any]]:
        mapping: dict[int, dict[str, Any]] = {}
        for row in store.rows(ARCHIVE_MODULE):
            try:
                mapping[int(row.get("审计记录id", row.get("id", 0)))] = row
            except (TypeError, ValueError):
                continue
        return mapping

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        source_text = _text(values.get("变更内容"))
        # 新事件也写版本化差异，避免迁移期查询同时命中两种格式。
        entry["变更差异"] = normalize_changes(values.get("变更差异") or source_text)
        entry["源文本归档"] = source_text
        entry["内容版本"] = VERSION_DIFF
        entry["迁移批次"] = None
        entry["迁移时间"] = None
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._decorate(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据审计可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._decorate(entry), f"审计记录已{action}"

    # -------------------------------------------------------------- 迁移批次
    def _next_batch_no(self) -> tuple[int, str]:
        batches = store.rows(BATCH_MODULE)
        next_id = max((int(batch.get("id", 0)) for batch in batches), default=0) + 1
        return next_id, f"BATCH-{next_id:04d}"

    def _pipeline(self, failed_step: str | None = None) -> list[dict[str, str]]:
        steps = [
            "读取既有审计记录",
            "读取操作人记录归档明细",
            "解析旧版审计文本为字段差异",
            "写入版本化差异并保留操作人、对象引用",
            "校验迁移结果与批次归属",
        ]
        pipeline: list[dict[str, str]] = []
        blocked = False
        for step in steps:
            if blocked:
                pipeline.append({"步骤": step, "状态": "已回滚"})
            elif failed_step == step:
                pipeline.append({"步骤": step, "状态": "失败"})
                blocked = True
            else:
                pipeline.append({"步骤": step, "状态": "已完成"})
        return pipeline

    def _batch_meta(
        self,
        *,
        batch_id: int,
        batch_no: str,
        batch_label: str,
        batch_status: str,
        submitted_by: str,
        archived_input: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        stats: dict[str, int],
        fingerprint: str,
        strict: bool,
        conflict_policy: str,
        message: str = "",
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": batch_id,
            "批次编号": batch_no,
            "批次名称": batch_label or f"旧版审计文本迁移 {batch_no}",
            "批次状态": batch_status,
            "schema版本": SCHEMA_VERSION,
            "提交人": submitted_by,
            "创建时间": now_text(),
            "输入来源": ["audit.变更内容（旧版审计文本）", "audit_operator_archive（操作人记录归档明细）"],
            "归档输入": archived_input,
            "迁移统计": stats,
            "冲突处理范围": [
                "记录级：目标记录已迁移时，按冲突策略跳过或整批失败，不覆盖既有有效差异",
                "批次级：相同请求指纹重复提交时直接返回既有批次，只保留一份有效结果",
            ],
            "冲突明细": conflicts,
            "数据库依赖": [MODULE, ARCHIVE_MODULE],
            "构建流水线": self._pipeline("解析旧版审计文本为字段差异" if batch_status == BATCH_FAILED else None),
            "回滚入口": f"/api/audit/migration-batches/{batch_no}/rollback",
            "严格模式": strict,
            "冲突策略": CONFLICT_POLICIES.get(conflict_policy, conflict_policy),
            "请求指纹": fingerprint,
            "失败原因": message if batch_status in (BATCH_FAILED, BATCH_ROLLED_BACK) else "",
            "回滚时间": (rollback or {}).get("回滚时间"),
            "回滚记录数": (rollback or {}).get("回滚记录数", 0),
            "回滚冲突": (rollback or {}).get("回滚冲突", []),
        }

    def list_batches(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(batch) for batch in sorted(
            store.rows(BATCH_MODULE), key=lambda item: int(item.get("id", 0)))]

    def get_batch(self, batch_ref: str | int) -> dict[str, Any] | None:
        target = str(batch_ref).strip()
        for batch in store.rows(BATCH_MODULE):
            if str(batch.get("id")) == target or _text(batch.get("批次编号")) == target:
                return batch
        return None

    def _find_batch(self, batch_ref: str | int) -> dict[str, Any] | None:
        batch = self.get_batch(batch_ref)
        return copy.deepcopy(batch) if batch else None

    def migrate(
        self,
        *,
        batch_label: str = "",
        record_ids: list[int] | None = None,
        scope: str = "legacy",
        strict: bool = False,
        conflict_policy: str = "skip",
        submitted_by: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        """读取既有数据后，把旧版文本迁移到版本化差异结构。

        整批写在一个事务里：任一条解析失败或操作人缺失，全部记录保持旧版文本。
        """
        record_ids = [int(item) for item in (record_ids or [])]
        scope = scope if scope in {"legacy", "all", "selected"} else "legacy"
        conflict_policy = conflict_policy if conflict_policy in CONFLICT_POLICIES else "skip"
        rows = store.rows(MODULE)

        if scope == "selected":
            target_ids = [int(row["id"]) for row in rows if int(row.get("id", 0)) in set(record_ids)]
        elif scope == "all":
            target_ids = [int(row.get("id", 0)) for row in rows]
        else:
            target_ids = [int(row["id"]) for row in rows if self._content_version(row) != VERSION_DIFF]

        if not target_ids:
            committed = [batch for batch in self.list_batches()
                         if batch.get("批次状态") == BATCH_COMMITTED]
            if committed:
                return committed[-1], "旧版审计文本已全部迁移，返回既有批次，只保留一份有效结果"
            return None, "没有需要迁移的旧版审计文本"

        targets = [row for row in rows if int(row.get("id", 0)) in set(target_ids)]
        fingerprint_payload = {
            "records": sorted((int(row.get("id", 0)), _text(row.get("变更内容"))) for row in targets),
            "strict": strict,
            "scope": scope,
            "conflict_policy": conflict_policy,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for batch in self.list_batches():
            if batch.get("批次状态") == BATCH_COMMITTED and batch.get("请求指纹") == fingerprint:
                return batch, "重复提交命中既有迁移批次，只保留一份有效结果"

        archive_by_id = self._archive_rows()
        batch_id, batch_no = self._next_batch_no()
        archived_input = [
            self._archive_input(row, archive_by_id.get(int(row.get("id", 0))), batch_no)
            for row in targets
        ]
        conflicts: list[dict[str, Any]] = []
        for row in targets:
            if self._content_version(row) == VERSION_DIFF:
                conflicts.append({
                    "范围": "记录级",
                    "记录编号": row.get("记录编号"),
                    "类型": "记录已迁移",
                    "说明": f"已归属批次 {_text(row.get('迁移批次')) or '实时写入'}，按「{CONFLICT_POLICIES[conflict_policy]}」处理",
                })
        if conflict_policy == "fail" and conflicts:
            stats = {"目标记录数": len(targets), "已迁移": 0, "跳过": 0, "冲突": len(conflicts)}
            batch = self._batch_meta(
                batch_id=batch_id, batch_no=batch_no, batch_label=batch_label,
                batch_status=BATCH_FAILED, submitted_by=submitted_by,
                archived_input=archived_input, conflicts=conflicts, stats=stats,
                fingerprint=fingerprint, strict=strict, conflict_policy=conflict_policy,
                message="目标记录已迁移，按冲突策略整批失败，未写入任何差异",
            )
            store.rows(BATCH_MODULE).append(batch)
            return None, f"迁移失败：{len(conflicts)} 条记录已迁移，整批未写入"

        pending = [row for row in targets if self._content_version(row) != VERSION_DIFF]
        if not pending:
            # 目标都已是版本化差异：不再写空批次，避免重复提交留下多份结果。
            committed = [item for item in self.list_batches()
                         if item.get("批次状态") == BATCH_COMMITTED]
            if committed:
                return committed[-1], "目标记录已是版本化差异，返回既有批次，只保留一份有效结果"
            return None, "没有需要迁移的旧版审计文本"

        stats = {"目标记录数": len(targets), "已迁移": 0, "跳过": 0, "冲突": len(conflicts)}
        batch = self._batch_meta(
            batch_id=batch_id, batch_no=batch_no, batch_label=batch_label,
            batch_status=BATCH_COMMITTED, submitted_by=submitted_by,
            archived_input=archived_input, conflicts=conflicts, stats=stats,
            fingerprint=fingerprint, strict=strict, conflict_policy=conflict_policy,
        )

        def mutate(tables: dict[str, list[dict[str, Any]]]) -> None:
            audit_rows = tables.setdefault(MODULE, [])
            archive_rows = tables.setdefault(ARCHIVE_MODULE, [])
            archive_index: dict[int, dict[str, Any]] = {}
            for archive in archive_rows:
                try:
                    archive_index[int(archive.get("审计记录id", archive.get("id", 0)))] = archive
                except (TypeError, ValueError):
                    continue
            by_id = {int(row.get("id", 0)): row for row in audit_rows}
            migrated = 0
            skipped = 0
            for record_id in target_ids:
                row = by_id.get(record_id)
                if row is None:
                    raise ValueError(f"审计记录 {record_id} 在迁移过程中被移除")
                if self._content_version(row) == VERSION_DIFF:
                    skipped += 1
                    continue
                source_text = _text(row.get("变更内容"))
                changes = parse_legacy_text(source_text)
                if strict and _is_unstructured(changes):
                    raise ValueError(
                        f"记录 {_text(row.get('记录编号')) or record_id} 的旧版文本无法解析为字段级差异")
                archive = archive_index.get(record_id) or {}
                operator = _text(row.get("操作人")) or _text(archive.get("操作人"))
                object_ref = _text(row.get("操作对象")) or _text(archive.get("操作对象"))
                if not operator:
                    raise ValueError(
                        f"记录 {_text(row.get('记录编号')) or record_id} 缺少操作人，归档明细中也没有可用值")
                if not object_ref:
                    raise ValueError(
                        f"记录 {_text(row.get('记录编号')) or record_id} 缺少操作对象，无法保留对象引用")
                row["操作人"] = operator
                row["操作对象"] = object_ref
                row["变更差异"] = changes
                row["源文本归档"] = source_text
                row["内容版本"] = VERSION_DIFF
                row["迁移批次"] = batch_no
                row["迁移时间"] = now_text()
                migrated += 1
            stats["已迁移"] = migrated
            stats["跳过"] = skipped
            batch["迁移统计"] = dict(stats)
            batch["构建流水线"] = self._pipeline()
            batch["归档输入"] = self._batch_archived_input(audit_rows, archived_input)
            tables.setdefault(BATCH_MODULE, []).append(batch)

        try:
            store.transaction(f"audit migration {batch_no}", mutate)
        except AuditMigrationError as error:
            failed = self._batch_meta(
                batch_id=batch_id, batch_no=batch_no, batch_label=batch_label,
                batch_status=BATCH_FAILED, submitted_by=submitted_by,
                archived_input=archived_input, conflicts=conflicts, stats=stats,
                fingerprint=fingerprint, strict=strict, conflict_policy=conflict_policy,
                message=error.batch.get("message", "迁移失败"),
            )
            store.rows(BATCH_MODULE).append(failed)
            return None, f"迁移失败并已整批回滚：{error.batch.get('message', '未知原因')}"

        return batch, f"迁移批次 {batch_no} 已完成，共迁移 {stats['已迁移']} 条记录"

    def _archive_input(
        self, row: dict[str, Any], archive: dict[str, Any] | None, batch_no: str
    ) -> dict[str, Any]:
        return {
            "审计记录id": int(row.get("id", 0)),
            "记录编号": row.get("记录编号"),
            "源文本": _text(row.get("变更内容")),
            "操作人": _text(row.get("操作人")) or _text((archive or {}).get("操作人")),
            "操作对象": _text(row.get("操作对象")) or _text((archive or {}).get("操作对象")),
            "操作人归档来源": _text((archive or {}).get("归档来源")),
            "操作人归档时间": _text((archive or {}).get("归档时间")),
            "操作人归档明细": copy.deepcopy((archive or {}).get("归档明细") or {}),
            "迁移前内容版本": self._content_version(row),
            "迁移前变更差异": copy.deepcopy(row.get("变更差异") or []),
            "迁移前迁移批次": row.get("迁移批次"),
            "迁移前迁移时间": row.get("迁移时间"),
            "迁移前操作人": _text(row.get("操作人")),
            "迁移前操作对象": _text(row.get("操作对象")),
            "迁移批次": batch_no,
        }

    def _batch_archived_input(
        self, audit_rows: list[dict[str, Any]], archived_input: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        by_id = {int(row.get("id", 0)): row for row in audit_rows}
        for item in archived_input:
            current = dict(item)
            row = by_id.get(int(item.get("审计记录id", 0)))
            if row is not None:
                current["迁移后内容版本"] = self._content_version(row)
                current["迁移后操作人"] = _text(row.get("操作人"))
                current["迁移后操作对象"] = _text(row.get("操作对象"))
            snapshot.append(current)
        return snapshot

    # -------------------------------------------------------------- 差异消费
    def diff_for_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        entry = self._decorate(row)
        legacy = entry["内容版本"] != VERSION_DIFF
        batch = self._find_batch(row.get("迁移批次")) if row.get("迁移批次") else None
        archive = self._archive_rows().get(entry_id)
        verification = self._verify(entry, batch, archive)
        return {
            "entry": entry,
            "旧版文本": _text(row.get("变更内容")),
            "变更差异": entry["变更差异"],
            # 未迁移记录只给预览，不写回，避免查询结果混出旧格式和新格式。
            "预览差异": parse_legacy_text(row.get("变更内容")) if legacy else [],
            "待迁移": legacy,
            "差异摘要": entry["差异摘要"],
            "内容版本": entry["内容版本"],
            "迁移批次": batch,
            "操作人归档": copy.deepcopy(archive),
            "校验": verification,
        }, ""

    def verification_for(self, entry_id: int) -> dict[str, Any]:
        """导出与页面共用的核对口径：操作人、对象引用、差异结构与归档一致性。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return {"ok": False, "检查项": [], "问题": ["记录不存在"], "核对时间": now_text()}
        return self._verify(
            self._decorate(row),
            self._find_batch(row.get("迁移批次")) if row.get("迁移批次") else None,
            self._archive_rows().get(entry_id),
        )

    def _verify(
        self, entry: dict[str, Any], batch: dict[str, Any] | None, archive: dict[str, Any] | None
    ) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        issues: list[str] = []
        operator = _text(entry.get("操作人"))
        object_ref = _text(entry.get("操作对象"))
        checks.append({
            "检查项": "操作人保留",
            "结果": "通过" if operator else "未通过",
            "说明": operator or "迁移后操作人为空",
        })
        if not operator:
            issues.append("操作人丢失")
        checks.append({
            "检查项": "对象引用保留",
            "结果": "通过" if object_ref else "未通过",
            "说明": object_ref or "迁移后操作对象为空",
        })
        if not object_ref:
            issues.append("操作对象引用丢失")
        changes = entry.get("变更差异") or []
        structured = bool(changes) and all(
            isinstance(item, dict) and item.get("field") and ("before" in item) and ("after" in item)
            for item in changes)
        checks.append({
            "检查项": "差异结构完整",
            "结果": "通过" if structured else "未通过",
            "说明": "字段级差异可被页面直接消费" if structured else "尚未迁移到字段级差异",
        })
        if not structured:
            issues.append("差异结构不完整")
        if batch:
            checks.append({
                "检查项": "批次归属",
                "结果": "通过" if entry.get("迁移批次") == batch.get("批次编号") else "未通过",
                "说明": f"记录归属批次 {entry.get('迁移批次')}",
            })
        else:
            checks.append({
                "检查项": "批次归属",
                "结果": "通过" if entry.get("内容版本") == VERSION_DIFF else "未通过",
                "说明": "实时写入的版本化差异，无需迁移批次",
            })
        if archive:
            archive_operator = _text(archive.get("操作人"))
            archive_object = _text(archive.get("操作对象"))
            matched = (not archive_operator or archive_operator == operator) and (
                not archive_object or archive_object == object_ref)
            checks.append({
                "检查项": "归档明细一致",
                "结果": "通过" if matched else "未通过",
                "说明": "操作人与操作对象和归档明细一致" if matched else "记录与操作人归档明细不一致",
            })
            if not matched:
                issues.append("归档明细不一致")
        else:
            checks.append({"检查项": "归档明细一致", "结果": "通过", "说明": "无归档明细，跳过比对"})
        return {"ok": not issues, "检查项": checks, "问题": issues, "核对时间": now_text()}

    # -------------------------------------------------------------- 批次明细
    def batch_records(self, batch_ref: str | int) -> tuple[dict[str, Any] | None, str]:
        batch = self._find_batch(batch_ref)
        if batch is None:
            return None, f"迁移批次 {batch_ref} 不存在"
        by_id = {int(row.get("id", 0)): row for row in store.rows(MODULE)}
        items: list[dict[str, Any]] = []
        for archive in batch.get("归档输入", []):
            record_id = int(archive.get("审计记录id", 0))
            row = by_id.get(record_id)
            current = self._decorate(row) if row else None
            items.append({
                "审计记录id": record_id,
                "记录编号": archive.get("记录编号"),
                "旧版文本": archive.get("源文本"),
                "源文本归档": current.get("源文本归档") if current else archive.get("源文本"),
                "变更差异": current.get("变更差异") if current else [],
                "差异摘要": current.get("差异摘要") if current else "",
                "当前内容版本": current.get("内容版本") if current else "记录不存在",
                "操作人": current.get("操作人") if current else archive.get("操作人"),
                "操作对象": current.get("操作对象") if current else archive.get("操作对象"),
                "仍归属本批次": bool(current and current.get("迁移批次") == batch.get("批次编号")),
                "归档输入": copy.deepcopy(archive),
                "校验": self._verify(current, batch, self._archive_rows().get(record_id)) if current else {
                    "ok": False, "检查项": [], "问题": ["记录不存在"], "核对时间": now_text()},
            })
        return {"batch": batch, "items": items, "total": len(items)}, ""

    # ---------------------------------------------------------------- 回滚
    def rollback_batch(self, batch_ref: str | int) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_batch(batch_ref)
        if batch is None:
            return None, f"迁移批次 {batch_ref} 不存在"
        batch_no = _text(batch.get("批次编号"))
        if batch.get("批次状态") == BATCH_ROLLED_BACK:
            return self._find_batch(batch_no), f"迁移批次 {batch_no} 已回滚，未重复写入"
        if batch.get("批次状态") != BATCH_COMMITTED:
            return None, f"迁移批次 {batch_no} 状态为「{batch.get('批次状态')}」，没有可回滚的差异"

        by_id = {int(row.get("id", 0)): row for row in store.rows(MODULE)}
        rollback_targets: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for archive in batch.get("归档输入", []):
            record_id = int(archive.get("审计记录id", 0))
            row = by_id.get(record_id)
            if row is None:
                conflicts.append({"范围": "记录级", "记录编号": archive.get("记录编号"),
                                  "类型": "记录不存在", "说明": "已无法回滚"})
                continue
            if _text(row.get("迁移批次")) != batch_no or self._content_version(row) != VERSION_DIFF:
                conflicts.append({
                    "范围": "记录级", "记录编号": archive.get("记录编号"),
                    "类型": "已被后续批次接管",
                    "说明": f"当前归属批次 {_text(row.get('迁移批次')) or '无'}，保留现有差异",
                })
                continue
            rollback_targets.append(archive)
        if not rollback_targets and conflicts:
            return None, f"迁移批次 {batch_no} 没有可回滚的记录，{len(conflicts)} 条已被后续变更接管"

        rollback_info = {
            "回滚时间": now_text(),
            "回滚记录数": len(rollback_targets),
            "回滚冲突": conflicts,
        }

        def mutate(tables: dict[str, list[dict[str, Any]]]) -> None:
            audit_rows = tables.setdefault(MODULE, [])
            current = {int(row.get("id", 0)): row for row in audit_rows}
            for archive in rollback_targets:
                row = current[int(archive.get("审计记录id", 0))]
                row["内容版本"] = archive.get("迁移前内容版本") or VERSION_LEGACY
                row["变更差异"] = copy.deepcopy(archive.get("迁移前变更差异") or [])
                row["迁移批次"] = archive.get("迁移前迁移批次")
                row["迁移时间"] = archive.get("迁移前迁移时间")
                row["源文本归档"] = row.get("源文本归档") or archive.get("源文本")
                # 回滚只撤内容版本，操作人与对象引用继续保持可追溯。
                row["操作人"] = _text(row.get("操作人")) or _text(archive.get("操作人"))
                row["操作对象"] = _text(row.get("操作对象")) or _text(archive.get("操作对象"))
                row["回滚批次"] = batch_no
                row["回滚时间"] = rollback_info["回滚时间"]
            target = next((item for item in tables.setdefault(BATCH_MODULE, [])
                           if _text(item.get("批次编号")) == batch_no), None)
            if target is not None:
                target["批次状态"] = BATCH_ROLLED_BACK
                target["回滚时间"] = rollback_info["回滚时间"]
                target["回滚记录数"] = rollback_info["回滚记录数"]
                target["回滚冲突"] = conflicts
                target["失败原因"] = ""

        store.transaction(f"audit rollback {batch_no}", mutate)
        return self._find_batch(batch_no), (
            f"迁移批次 {batch_no} 已回滚 {len(rollback_targets)} 条记录"
            + (f"，{len(conflicts)} 条因后续变更未回滚" if conflicts else "")
        )

    # ---------------------------------------------------------------- 导出
    def export_rows(
        self, *, filters: dict[str, str] | None = None, keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        items, _ = self.list_entries(filters=filters, keyword=keyword, status=status,
                                     page=1, size=1_000_000)
        rows: list[dict[str, Any]] = []
        for item in items:
            row = dict(item)
            row["差异摘要"] = item.get("差异摘要", "")
            rows.append(row)
        return rows
