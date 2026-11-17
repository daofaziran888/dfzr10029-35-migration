"""数据审计接口：登记、状态流转、旧版文本迁移批次与差异核对。

路由顺序约定：/export 与 /migration-batches 必须声明在 /{entry_id} 之前，
否则会被当成记录编号解析。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.audit import EXPORT_FIELDS, LIST_FIELDS, AuditService

router = APIRouter(prefix="/api/audit", tags=["数据审计"])

service = AuditService()

STATUSES = ["已记录", "已审计", "异常"]
CONTENT_VERSIONS = ["旧版文本", "版本化差异"]
CSV_HEADER = ["id", *EXPORT_FIELDS, "status", "变更差异", "差异检查"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def _csv_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已审计、异常"),
    batch_id: str | None = Query(default=None, description="按迁移批次编号过滤"),
    content_version: str | None = Query(default=None, description="旧版文本、版本化差异"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、记录编号、状态、迁移批次与内容版本过滤数据审计列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        batch_id=batch_id, content_version=content_version, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    content_version: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；保留旧版文本、差异摘要与迁移批次来源依据。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status,
        batch_id=batch_id, content_version=content_version, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for item in items:
        diff_text = "；".join(
            f"{change.get('field', '')}: {change.get('before', '') or '∅'} -> {change.get('after', '')}"
            for change in item.get("变更差异", []))
        check = service.verification_for(int(item.get("id", 0)))
        check_text = "通过" if check["ok"] else "；".join(check["问题"])
        row = [
            item.get("id"),
            *[item.get(field, "") for field in EXPORT_FIELDS],
            item.get("status", ""),
            diff_text,
            check_text,
        ]
        writer.writerow([_csv_text(value) for value in row])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'})


@router.get("/migration-batches")
def list_migration_batches() -> dict[str, Any]:
    """迁移批次列表：带归档输入、冲突明细与回滚入口。"""
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.post("/migration-batches", response_model=ActionResult)
def create_migration_batch(payload: dict[str, Any] | None = None) -> ActionResult:
    """读取既有审计记录后迁移旧版文本；失败整批回滚并保留批次归档输入。"""
    payload = payload or {}
    record_ids = payload.get("record_ids") or []
    if not isinstance(record_ids, list):
        return ActionResult(ok=False, message="record_ids 必须是审计记录 id 数组")
    batch, message = service.migrate(
        batch_label=str(payload.get("batch_label") or ""),
        record_ids=[int(item) for item in record_ids if str(item).strip().isdigit()],
        scope=str(payload.get("scope") or "legacy"),
        strict=bool(payload.get("strict")),
        conflict_policy=str(payload.get("conflict_policy") or "skip"),
        submitted_by=str(payload.get("submitted_by") or "值班管理员"),
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/migration-batches/{batch_ref}")
def get_migration_batch(batch_ref: str) -> dict[str, Any]:
    """读取单个迁移批次；失败批次也能读到归档输入和失败原因。"""
    batch = service.get_batch(batch_ref)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"迁移批次 {batch_ref} 不存在")
    return batch


@router.get("/migration-batches/{batch_ref}/records")
def get_batch_records(batch_ref: str) -> dict[str, Any]:
    """审计差异接口编排：按批次返回旧文本与新差异的对照和核对结论。"""
    payload, message = service.batch_records(batch_ref)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.post("/migration-batches/{batch_ref}/rollback", response_model=ActionResult)
def rollback_migration_batch(batch_ref: str) -> ActionResult:
    """把批次内仍是该批次结果的记录回滚到旧版文本；被后续批次接管的记录保留现状。"""
    batch, message = service.rollback_batch(batch_ref)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/{entry_id}/diff")
def get_entry_diff(entry_id: int) -> dict[str, Any]:
    """消费单条记录的差异结构，并核对操作人、对象引用与归档明细。"""
    payload, message = service.diff_for_entry(entry_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条审计记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条审计记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="审计记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条审计记录执行提交审计、标记异常；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
