"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from app.seed import SEED_ROWS

Mutation = Callable[[dict[str, list[dict[str, Any]]]], None]

# 迁移批次与操作人归档是审计模块的内部依赖表，不参与业务模块概览。
SUPPORT_MODULES = {"audit_migration_batch", "audit_operator_archive"}


class AuditMigrationError(RuntimeError):
    """迁移批次整体回滚；batch 里带着可直接展示给审计页的失败原因。"""

    def __init__(self, batch: dict[str, Any]) -> None:
        super().__init__(batch.get("message", "迁移批次回滚"))
        self.batch = batch


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._journal: list[dict[str, Any]] = []

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def business_module_names(self) -> list[str]:
        return [name for name in self.module_names() if name not in SUPPORT_MODULES]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def transaction(self, label: str, mutate: Mutation) -> dict[str, Any]:
        """把一次批量变更当成一个整体执行：失败时整表回滚，并留下可审计的批次流水。

        迁移场景里最怕「一半旧文本、一半新差异」——静态查询会同时命中两种格式。
        这里先对涉及的表做快照，任何异常都恢复快照并把失败原因记进批次流水。
        """
        before = copy.deepcopy(self._tables)
        try:
            mutate(self._tables)
        except Exception as error:  # noqa: BLE001 - 回滚后要把原始原因交给调用方
            self._tables = before
            batch = {
                "batch_id": self._next_batch_id(),
                "label": label,
                "status": "rolled_back",
                "message": f"{type(error).__name__}: {error}",
            }
            self._journal.append(batch)
            raise AuditMigrationError(batch) from error
        batch = {"batch_id": self._next_batch_id(), "label": label, "status": "committed", "message": ""}
        self._journal.append(batch)
        return batch

    def _next_batch_id(self) -> int:
        return len(self._journal) + 1

    def journal(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._journal]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.business_module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
