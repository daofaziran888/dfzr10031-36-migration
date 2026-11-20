#!/usr/bin/env python3
"""审计时间迁移端到端验证：换算、重跑（幂等）与撤销（回滚）前后的快照合计。

场景覆盖：

1. 取仓库原审计导出中的时间文本（种子数据 2026-09-01/02/03）整体迁移；
2. 同一操作地迟到两份午夜附近的存量记录：一份带时区偏移（00:05+08:00），
   一份只有本地时间（23:55，无法唯一映射）——验证"先切换可解析记录"：
   00:05 立即进入新日期序列，23:55 只进迁移未决清单，不进绝对时间序列；
3. 再录入缺失时间与跨日时间（-05:00 跨到 UTC 次日）；
4. 重跑全部迁移，验证幂等（快照合计与证据不变）；
5. 回填失败重试：验证已确认的时区换算证据不被改写；
6. 回滚迟到记录所在批次，验证快照合计恢复、旧清单操作顺序仍可核对。

运行：python3 backend/scripts/verify_audit_time_migration.py
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.services.audit_time_migration import (  # noqa: E402
    PRECISION_ABSOLUTE,
    PRECISION_DAY,
    PRECISION_LOCAL_UNRESOLVED,
    PRECISION_MISSING,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    AuditTimeMigrationStore,
    evidence_hash,
    parse_operation_time,
    source_hash,
)
from app.store import store  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if condition else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not condition:
        FAILURES.append(label)


def day_counts(snapshot: dict) -> dict[str, int]:
    return {bucket["day"]: bucket["count"] for bucket in snapshot["日序列"]}


def day_members(snapshot: dict, day: str) -> list[str]:
    for bucket in snapshot["日序列"]:
        if bucket["day"] == day:
            return [item["记录编号"] for item in bucket["items"]]
    return []


def pending_ids(snapshot: dict) -> list[str]:
    return [item["记录编号"] for item in snapshot["迁移未决清单"]]


def evidence_of(db: AuditTimeMigrationStore, op_id: str) -> str:
    record = db.get_record(op_id)
    assert record is not None, op_id
    return record["evidence_hash"]


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="audit-time-migration-")
    db_path = os.path.join(tmpdir, "audit_time_compat.db")
    db = AuditTimeMigrationStore(db_path)

    # ---------- 第 0 步：换算前的旧清单口径 ----------
    audit_rows = store.rows("audit")
    seed_entries = [
        {"op_id": row["记录编号"], "source_seq": int(row["id"]), "original_text": row["操作时间"]}
        for row in audit_rows
    ]
    legacy_before: dict[str, int] = {}
    for entry in seed_entries:
        legacy_before[entry["original_text"]] = legacy_before.get(entry["original_text"], 0) + 1
    print("== 换算前：旧清单（原文本）==")
    print("   旧材料日计数:", legacy_before, " 顺序:", [e["op_id"] for e in seed_entries])
    check("换算前旧清单为 3 条日期文本", legacy_before == {"2026-09-01": 1, "2026-09-02": 1, "2026-09-03": 1})

    # ---------- 第 1 步：原审计导出整体迁移（批次 M1） ----------
    m1 = db.migrate(seed_entries, note="M1 原审计导出")
    snap1 = db.snapshot_view()
    print("\n== 第 1 步：原审计导出迁移（批次 M1）==")
    print("   日序列:", day_counts(snap1), " 未决:", pending_ids(snap1))
    check("M1 三条记录全部到日精度确认", m1["summary"]["inserted"] == 3 and not snap1["迁移未决清单"])
    check(
        "M1 日序列与旧材料计数一致（纯日期文本不产生口径差异）",
        day_counts(snap1) == {"2026-09-01": 1, "2026-09-02": 1, "2026-09-03": 1},
    )
    check(
        "M1 记录精度均为 day（可确定到日，不产生绝对时间）",
        all(item["精度"] == PRECISION_DAY for item in db.compat_index()),
    )

    # ---------- 第 2 步：同一操作地迟到的午夜对（批次 M2） ----------
    print("\n== 第 2 步：同一操作地迟到的午夜对（批次 M2）——先切换可解析记录 ==")
    m2 = db.migrate(
        [
            {"op_id": "AUDI-0004", "source_seq": 4, "original_text": "2026-09-02T00:05:00+08:00"},
            {"op_id": "AUDI-0005", "source_seq": 5, "original_text": "2026-09-01 23:55"},
        ],
        note="M2 同一操作地迟到的午夜对",
    )
    snap2 = db.snapshot_view()
    rec4 = db.get_record("AUDI-0004")
    rec5 = db.get_record("AUDI-0005")
    print("   AUDI-0004:", rec4["precision"], rec4["utc_time"], " UTC日:", rec4["utc_day"])
    print("   AUDI-0005:", rec5["precision"], " 未决原因:", rec5["reason"])
    print("   日序列:", day_counts(snap2), " 未决:", pending_ids(snap2))
    print("   旧材料日计数:", db.legacy_day_counts())

    check(
        "00:05 带偏移记录换算为绝对时间（UTC 前一日的 16:05）",
        rec4["precision"] == PRECISION_ABSOLUTE
        and rec4["status"] == STATUS_CONFIRMED
        and rec4["utc_time"] == "2026-09-01T16:05:00+00:00"
        and rec4["utc_day"] == "2026-09-01",
    )
    check(
        "本地 23:55 时区不明，进迁移未决清单",
        rec5["precision"] == PRECISION_LOCAL_UNRESOLVED and rec5["status"] == STATUS_PENDING,
    )
    check(
        "新日期序列先计 00:05：AUDI-0004 计入 2026-09-01（UTC 日）",
        "AUDI-0004" in day_members(snap2, "2026-09-01"),
    )
    check(
        "未确认时区的 23:55 没有进入任何日序列（不混入绝对时间序列）",
        not any("AUDI-0005" in day_members(snap2, d) for d in day_counts(snap2))
        and "AUDI-0005" in pending_ids(snap2),
    )
    check(
        "旧清单里的顺序不变：AUDI-0001..0005 按旧清单序号排列",
        [item["记录编号"] for item in db.legacy_order()] == ["AUDI-0001", "AUDI-0002", "AUDI-0003", "AUDI-0004", "AUDI-0005"],
    )
    check(
        "旧材料两天各自的计数保留：09-01 两条（0001+0005）、09-02 两条（0002+0004）",
        db.legacy_day_counts().get("2026-09-01") == 2 and db.legacy_day_counts().get("2026-09-02") == 2,
        "直接换算会改变这两天的计数，因此新旧口径分列、不静默合并",
    )
    check(
        "新口径 2026-09-01 只含已确认的 0001(到日)+0004(绝对)，不含未决的 0005",
        sorted(day_members(snap2, "2026-09-01")) == ["AUDI-0001", "AUDI-0004"],
    )

    # ---------- 第 3 步：录入缺失与跨日时间（批次 M3） ----------
    print("\n== 第 3 步：缺失时间与跨日时间（批次 M3） ==")
    db.migrate(
        [
            {"op_id": "AUDI-0006", "source_seq": 6, "original_text": ""},
            {"op_id": "AUDI-0007", "source_seq": 7, "original_text": "2026-09-03T23:30:00-05:00"},
        ],
        note="M3 缺失与跨日",
    )
    snap3 = db.snapshot_view()
    rec7 = db.get_record("AUDI-0007")
    print("   AUDI-0007:", rec7["precision"], rec7["utc_time"], " UTC日:", rec7["utc_day"])
    print("   日序列:", day_counts(snap3), " 未决:", pending_ids(snap3))
    check("缺失时间进未决清单（missing）", db.get_record("AUDI-0006")["precision"] == PRECISION_MISSING)
    check(
        "跨日时间换算到 UTC 次日（09-03 23:30-05:00 → 09-04 04:30Z）",
        rec7["utc_time"] == "2026-09-04T04:30:00+00:00" and rec7["utc_day"] == "2026-09-04",
    )
    check("跨日记录计入新日期序列 2026-09-04", day_members(snap3, "2026-09-04") == ["AUDI-0007"])
    snapshot_after_convert = copy.deepcopy(snap3)
    print("   换算后快照合计:", snap3["合计"])

    # ---------- 第 4 步：重跑全部迁移，验证幂等 ----------
    print("\n== 第 4 步：重跑全部迁移（幂等） ==")
    all_entries = seed_entries + [
        {"op_id": "AUDI-0004", "source_seq": 4, "original_text": "2026-09-02T00:05:00+08:00"},
        {"op_id": "AUDI-0005", "source_seq": 5, "original_text": "2026-09-01 23:55"},
        {"op_id": "AUDI-0006", "source_seq": 6, "original_text": ""},
        {"op_id": "AUDI-0007", "source_seq": 7, "original_text": "2026-09-03T23:30:00-05:00"},
    ]
    evidence_before_rerun = {e["op_id"]: evidence_of(db, e["op_id"]) for e in all_entries}
    m4 = db.migrate(all_entries, note="M4 全量重跑")
    snap4 = db.snapshot_view()
    print("   重跑结果:", m4["summary"])
    check("同一操作编号重复迁移全部幂等（7 条均不变）", m4["summary"]["unchanged"] == 7 and m4["summary"]["inserted"] == 0)
    check("重跑后快照合计与换算后完全一致", snap4 == snapshot_after_convert)
    check(
        "重跑后每条记录的换算证据哈希不变",
        all(evidence_of(db, e["op_id"]) == evidence_before_rerun[e["op_id"]] for e in all_entries),
    )

    # ---------- 第 5 步：回填失败重试，不改写已确认证据 ----------
    print("\n== 第 5 步：回填与重试的证据保护 ==")
    evidence_4 = evidence_of(db, "AUDI-0004")
    # 5a. 缺失记录先用无效文本回填（仍然失败），再用带偏移文本回填（成功）
    db.migrate([{"op_id": "AUDI-0006", "source_seq": 6, "original_text": "时间待定"}], note="M5 回填失败")
    check("无效文本回填后仍未决（missing）", db.get_record("AUDI-0006")["status"] == STATUS_PENDING)
    db.migrate([{"op_id": "AUDI-0006", "source_seq": 6, "original_text": "2026-09-06T08:00:00+00:00"}], note="M6 回填成功")
    rec6 = db.get_record("AUDI-0006")
    check(
        "未决记录回填带偏移文本后转为已确认绝对时间",
        rec6["precision"] == PRECISION_ABSOLUTE and rec6["utc_time"] == "2026-09-06T08:00:00+00:00",
    )
    # 5b. 用冲突文本重试已确认记录：证据必须保持不变
    db.migrate([{"op_id": "AUDI-0004", "source_seq": 4, "original_text": "2026-09-02T00:05:00-05:00"}], note="M7 冲突重试")
    rec4_after = db.get_record("AUDI-0004")
    check(
        "已确认记录被冲突文本重试时保留原换算证据（UTC 仍是 16:05）",
        rec4_after["utc_time"] == "2026-09-01T16:05:00+00:00" and rec4_after["evidence_hash"] == evidence_4,
    )
    # 5c. 本地 23:55 用相同文本重试：仍是未决，不进入绝对时间序列
    db.migrate([{"op_id": "AUDI-0005", "source_seq": 5, "original_text": "2026-09-01 23:55"}], note="M8 未决重试")
    snap5 = db.snapshot_view()
    check(
        "23:55 重试后仍为未决，且仍不在任何日序列中",
        db.get_record("AUDI-0005")["status"] == STATUS_PENDING
        and "AUDI-0005" in pending_ids(snap5)
        and all("AUDI-0005" not in day_members(snap5, d) for d in day_counts(snap5)),
    )
    # 5d. 来源可核实：哈希能由原文重算
    rec6_after = db.get_record("AUDI-0006")
    check(
        "换算前后的来源可核实（来源哈希与证据哈希均可由原文重算）",
        rec6_after["source_hash"] == source_hash("AUDI-0006", "2026-09-06T08:00:00+00:00")
        and rec6_after["evidence_hash"] == evidence_hash("AUDI-0006", "2026-09-06T08:00:00+00:00", parse_operation_time("2026-09-06T08:00:00+00:00")),
    )
    snapshot_before_rollback = copy.deepcopy(db.snapshot_view())
    print("   撤销前快照合计:", snapshot_before_rollback["合计"])

    # ---------- 第 6 步：回滚 M2 批次 ----------
    print("\n== 第 6 步：回滚 M2（迟到的午夜对所在批次） ==")
    rb = db.rollback(m2["batch_id"])
    snap6 = db.snapshot_view()
    print("   ", rb["message"])
    print("   回滚后日序列:", day_counts(snap6), " 未决:", pending_ids(snap6))
    check("回滚撤出了 M2 的两条发布记录", rb["ok"] and rb["withdrawn"] == 2)
    check(
        "回滚后 2026-09-01 只剩到日记录 AUDI-0001（00:05 的绝对时间记录已撤出）",
        day_members(snap6, "2026-09-01") == ["AUDI-0001"],
    )
    check("回滚后未决清单不再包含 AUDI-0005", "AUDI-0005" not in pending_ids(snap6))
    check(
        "回滚后快照合计 = 撤销前合计 − M2 贡献（1 条已确认 + 1 条未决）",
        snap6["合计"]["已确认记录"] == snapshot_before_rollback["合计"]["已确认记录"] - 1
        and snap6["合计"]["未决记录"] == snapshot_before_rollback["合计"]["未决记录"] - 1,
    )
    legacy_after_rollback = db.legacy_order()
    check(
        "回滚后仍能核对旧记录的操作顺序（7 条原文按序号排列，M2 标记已回滚）",
        [item["记录编号"] for item in legacy_after_rollback] == [f"AUDI-000{i}" for i in range(1, 8)]
        and all(item["操作时间原文"] == expected for item, expected in zip(
            legacy_after_rollback,
            ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-02T00:05:00+08:00", "2026-09-01 23:55", "2026-09-06T08:00:00+00:00", "2026-09-03T23:30:00-05:00"],
        ))
        and {item["记录编号"]: item["迁移状态"] for item in legacy_after_rollback if item["记录编号"] in ("AUDI-0004", "AUDI-0005")} == {"AUDI-0004": "已回滚", "AUDI-0005": "已回滚"},
    )
    rb_again = db.rollback(m2["batch_id"])
    check("重复回滚同一批次幂等（不再撤出记录）", rb_again["ok"] and rb_again["withdrawn"] == 0)

    # ---------- 第 7 步：兼容数据库持久化与旧读取方式 ----------
    print("\n== 第 7 步：兼容数据库重开 + 旧读取方式保留 ==")
    index_before_close = db.compat_index()
    db.close()
    db2 = AuditTimeMigrationStore(db_path)
    check(
        "兼容数据库重开后索引一致（SQLite 持久化）",
        db2.compat_index() == index_before_close and db2.legacy_order() == legacy_after_rollback,
    )
    check(
        "旧读取方式保留：原审计导出的操作时间文本未被迁移改动",
        [row["操作时间"] for row in store.rows("audit")] == ["2026-09-01", "2026-09-02", "2026-09-03"],
    )

    # ---------- 汇总 ----------
    print("\n== 快照合计对照 ==")
    print("   换算前（旧材料口径）:", legacy_before)
    print("   换算后:", snapshot_after_convert["合计"])
    print("   重跑后:", snap4["合计"], "（与换算后一致）" if snap4 == snapshot_after_convert else "（不一致！）")
    print("   撤销后:", snap6["合计"])
    print(f"\n共 {CHECKS} 项检查，失败 {len(FAILURES)} 项")
    db2.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    if FAILURES:
        print("失败项:")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
