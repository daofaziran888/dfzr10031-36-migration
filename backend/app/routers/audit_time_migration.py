"""审计时间迁移接口：时间字段格式迁移、兼容索引、快照视图与批次回滚。

旧读取方式保持不变（/api/audit 及其导出仍返回原文）；这里提供的是并行的
新版换算结果：日序列、迁移未决清单、旧清单顺序核对与整批回滚。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, EntryPayload
from app.services.audit_time_migration import AuditTimeMigrationService

router = APIRouter(prefix="/api/audit/time-migration", tags=["数据审计时间迁移"])

service = AuditTimeMigrationService()


@router.get("/index")
def compat_index() -> dict[str, Any]:
    """审计时间兼容索引：发布中的新版记录，按操作编号关联原文与证据。"""
    items = service.migrations.compat_index()
    return {"total": len(items), "items": items}


@router.get("/snapshot-view")
def snapshot_view() -> dict[str, Any]:
    """数据快照视角：消费兼容索引，产出日序列、迁移未决清单与合计。"""
    return service.migrations.snapshot_view()


@router.get("/legacy-order")
def legacy_order() -> dict[str, Any]:
    """旧清单口径：按旧清单序号排列的原文记录与旧材料日计数，用于核对操作顺序。"""
    items = service.migrations.legacy_order()
    return {"total": len(items), "items": items, "旧材料日计数": service.migrations.legacy_day_counts()}


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """迁移批次列表：每批的发布/回滚状态与条目数。"""
    items = service.migrations.list_batches()
    return {"total": len(items), "items": items}


@router.post("/run", response_model=ActionResult)
def run_export_migration(payload: EntryPayload) -> ActionResult:
    """把当前审计导出清单整体迁移为一个批次；重复执行幂等。"""
    note = str(payload.values.get("note") or "").strip()
    result = service.run_export_migration(note=note)
    summary = result["summary"]
    message = (
        f"批次 {result['batch_id']} 迁移完成：新增 {summary['inserted']} 条，"
        f"不变 {summary['unchanged']} 条，重算 {summary['updated']} 条，"
        f"保留已确认证据 {summary['conflict_kept']} 条"
    )
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/entries", response_model=ActionResult)
def register_entry(payload: EntryPayload) -> ActionResult:
    """录入单条记录并立即换算（带偏移、缺失或跨日时间均可）。"""
    item, message = service.register_entry(payload.values)
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


@router.post("/rollback", response_model=ActionResult)
def rollback_batch(payload: EntryPayload) -> ActionResult:
    """回滚一个迁移批次：该批发布内容撤出快照视图，旧清单顺序仍可核对。"""
    batch_id = str(payload.values.get("batch_id") or payload.values.get("批次") or "").strip()
    if not batch_id:
        raise HTTPException(status_code=400, detail="缺少要回滚的批次号 batch_id")
    ok, message = service.rollback_batch(batch_id)
    return ActionResult(ok=ok, message=message)
