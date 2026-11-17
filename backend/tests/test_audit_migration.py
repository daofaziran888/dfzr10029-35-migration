"""数据审计迁移规则回归测试：不依赖 pytest，直接 python -m unittest 即可。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.audit import (  # noqa: E402
    BATCH_FAILED,
    BATCH_ROLLED_BACK,
    VERSION_DIFF,
    VERSION_LEGACY,
    AuditService,
)
from app.services.audit_diff import normalize_changes, parse_legacy_text  # noqa: E402
from app.store import store  # noqa: E402


class AuditDiffTextTest(unittest.TestCase):
    def test_arrow_and_change_words_share_one_migration_batch(self) -> None:
        text = "指标名称: 日活用户 -> 日活跃用户数；计算公式从 count(*) 改为 count(distinct user_id)"
        changes = parse_legacy_text(text)
        self.assertEqual([item["field"] for item in changes], ["指标名称", "计算公式"])
        self.assertEqual(changes[0]["before"], "日活用户")
        self.assertEqual(changes[1]["after"], "count(distinct user_id)")

    def test_json_archive_text_is_recognised(self) -> None:
        changes = normalize_changes('[{"字段": "刷新周期", "旧值": "每日", "新值": "每小时"}]')
        self.assertEqual(changes, [{"field": "刷新周期", "before": "每日", "after": "每小时"}])

    def test_free_text_degrades_to_single_change(self) -> None:
        changes = parse_legacy_text("按评审会结论统一调整了字段说明")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field"], "变更内容")
        self.assertEqual(changes[0]["after"], "按评审会结论统一调整了字段说明")


class AuditMigrationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从全新的演示数据开始，避免批次状态互相影响。
        from app.seed import SEED_ROWS

        for name in ("audit", "audit_migration_batch", "audit_operator_archive"):
            store._tables[name] = [dict(row) for row in SEED_ROWS.get(name, [])]
        store._journal.clear()
        self.service = AuditService()

    def test_migration_keeps_operator_object_and_versions_changes(self) -> None:
        batch, message = self.service.migrate(batch_label="首轮迁移")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["批次状态"], "已迁移")
        self.assertIn("共迁移 3 条记录", message)

        items, _ = self.service.list_entries(size=50)
        self.assertEqual({item["内容版本"] for item in items}, {VERSION_DIFF})
        # 第 2 条原本没有操作人，必须从操作人归档明细回填而不是丢失。
        by_code = {item["记录编号"]: item for item in items}
        self.assertEqual(by_code["AUDI-0002"]["操作人"], "陈思远")
        self.assertEqual(by_code["AUDI-0003"]["操作对象"], "DATA-0003 用户行为数据集")
        for item in items:
            self.assertEqual(item["迁移批次"], batch["批次编号"])

        diff_payload, _ = self.service.diff_for_entry(by_code["AUDI-0001"]["id"])
        self.assertTrue(diff_payload["校验"]["ok"])
        self.assertEqual(diff_payload["迁移批次"]["批次编号"], batch["批次编号"])

    def test_repeat_submission_returns_single_effective_batch(self) -> None:
        first, _ = self.service.migrate()
        second, message = self.service.migrate()
        self.assertEqual(first["批次编号"], second["批次编号"])
        committed = [batch for batch in self.service.list_batches()
                     if batch["批次状态"] == "已迁移"]
        self.assertEqual(len(committed), 1)
        self.assertIn("一份有效结果", message)

    def test_duplicate_submission_with_same_selection_is_deduplicated(self) -> None:
        first, _ = self.service.migrate(scope="selected", record_ids=[1, 2])
        second, message = self.service.migrate(scope="selected", record_ids=[1, 2])
        self.assertEqual(first["请求指纹"], second["请求指纹"])
        self.assertEqual(len(self.service.list_batches()), 1)
        self.assertIn("重复提交命中既有迁移批次", message)

    def test_failure_rolls_back_whole_batch_and_keeps_archive_input(self) -> None:
        # 删除第 3 条的操作人归档，迁移应因缺少操作人整批回滚。
        store.rows("audit_operator_archive").pop()
        batch, message = self.service.migrate(strict=False)
        self.assertIsNone(batch)
        self.assertIn("已整批回滚", message)

        batches = self.service.list_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["批次状态"], BATCH_FAILED)
        self.assertTrue(batches[0]["归档输入"])
        self.assertIn("构建流水线", batches[0])

        items, _ = self.service.list_entries(size=50)
        self.assertEqual({item["内容版本"] for item in items}, {VERSION_LEGACY})
        self.assertEqual(by_id(items, 1)["操作人"], "张启明")

    def test_strict_mode_rolls_back_unstructured_text(self) -> None:
        batch, message = self.service.migrate(strict=True)
        self.assertIsNone(batch)
        self.assertIn("无法解析为字段级差异", message)
        items, _ = self.service.list_entries(size=50)
        self.assertEqual({item["内容版本"] for item in items}, {VERSION_LEGACY})

    def test_conflict_policy_skips_or_fails_without_overwriting(self) -> None:
        self.service.migrate(scope="selected", record_ids=[1])
        first_round = by_id(self.service.list_entries(size=50)[0], 1)["变更差异"]

        # skip：已迁移的 1 号跳过，其余照常迁移，既有差异不被覆盖。
        batch, message = self.service.migrate(scope="all", conflict_policy="skip")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["迁移统计"]["跳过"], 1)
        self.assertEqual(batch["迁移统计"]["已迁移"], 2)
        self.assertEqual(by_id(self.service.list_entries(size=50)[0], 1)["变更差异"], first_round)

        # fail：回滚后重来一次，存在已迁移记录时整批失败，不写入任何差异。
        self.service.rollback_batch(batch["批次编号"])
        failed, message = self.service.migrate(scope="all", conflict_policy="fail")
        self.assertIsNone(failed)
        self.assertIn("整批未写入", message)
        statuses = [item["批次状态"] for item in self.service.list_batches()]
        self.assertIn(BATCH_FAILED, statuses)
        items = self.service.list_entries(size=50)[0]
        # 1 号仍归属首轮有效批次，2、3 号保持旧版文本。
        self.assertEqual(by_id(items, 1)["内容版本"], VERSION_DIFF)
        self.assertEqual(by_id(items, 1)["变更差异"], first_round)
        self.assertEqual(by_id(items, 2)["内容版本"], VERSION_LEGACY)
        self.assertEqual(by_id(items, 3)["内容版本"], VERSION_LEGACY)

    def test_rollback_restores_legacy_text_but_keeps_operator(self) -> None:
        batch, _ = self.service.migrate()
        rolled, message = self.service.rollback_batch(batch["批次编号"])
        self.assertIsNotNone(rolled)
        self.assertEqual(rolled["批次状态"], BATCH_ROLLED_BACK)

        items, _ = self.service.list_entries(size=50)
        self.assertEqual({item["内容版本"] for item in items}, {VERSION_LEGACY})
        for item in items:
            self.assertTrue(item["操作人"])
            self.assertTrue(item["操作对象"])
            self.assertEqual(item["迁移批次"], None)
        self.assertIn("3 条记录", message)

        again, message = self.service.rollback_batch(batch["批次编号"])
        self.assertEqual(again["批次状态"], BATCH_ROLLED_BACK)
        self.assertIn("未重复写入", message)

    def test_rollback_after_later_batch_takeover_keeps_later_result(self) -> None:
        first, _ = self.service.migrate()
        # 模拟后续批次接管了 1 号记录：回滚首轮批次时不能覆盖后来的有效差异。
        store.find("audit", 1)["迁移批次"] = "BATCH-9999"
        rolled, message = self.service.rollback_batch(first["批次编号"])
        self.assertEqual(rolled["批次状态"], BATCH_ROLLED_BACK)
        self.assertEqual(rolled["回滚记录数"], 2)
        self.assertEqual(len(rolled["回滚冲突"]), 1)
        self.assertIn("1 条因后续变更未回滚", message)

        items = self.service.list_entries(size=50)[0]
        taken_over = by_id(items, 1)
        self.assertEqual(taken_over["迁移批次"], "BATCH-9999")
        self.assertEqual(taken_over["内容版本"], VERSION_DIFF)
        for record_id in (2, 3):
            self.assertEqual(by_id(items, record_id)["内容版本"], VERSION_LEGACY)

    def test_new_events_write_versioned_diff_not_legacy_text(self) -> None:
        entry, missing = self.service.create_entry({
            "记录编号": "AUDI-0004",
            "操作类型": "新增指标",
            "操作人": "王若彤",
            "操作对象": "METR-0004 支付转化率",
            "变更内容": "指标名称: 支付率 -> 支付转化率",
        })
        self.assertEqual(missing, [])
        self.assertEqual(entry["内容版本"], VERSION_DIFF)
        self.assertEqual(entry["变更差异"][0]["field"], "指标名称")
        self.assertIsNone(entry["迁移批次"])

        diff_payload, _ = self.service.diff_for_entry(entry["id"])
        self.assertTrue(diff_payload["校验"]["ok"])


def by_id(items: list[dict], entry_id: int) -> dict:
    return next(item for item in items if int(item["id"]) == entry_id)


if __name__ == "__main__":
    unittest.main()
