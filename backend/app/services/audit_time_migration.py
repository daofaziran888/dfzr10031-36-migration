"""审计时间字段迁移：把导出清单里的操作时间文本换算成可按时区排序的新版记录。

背景：审计导出清单（旧清单）的 ``操作时间`` 一直是文本，既有 ``2026-09-01``
这样的日期，也有手工登记带进来的本地时间。新版记录要能按时区计算顺序，因此
每条旧文本都要经过一次可核实的换算，并按精度分级：

- ``absolute``：带时区偏移的时间（如 ``2026-09-02T00:05:00+08:00``），
  可换算为 UTC 绝对时间，进入已确认的绝对时间序列；
- ``day``：只有日期的文本（如 ``2026-09-01``），可确定到日，
  按原文的日计入日序列，但不产生绝对时间；
- ``local_unresolved``：只有本地时间、没有偏移量（如 ``2026-09-01 23:55``），
  时区不明，一律进迁移未决清单，绝不混入已确认的绝对时间序列；
- ``missing``：空文本或解析失败的文本，同样进入迁移未决清单。

同一操作地迟到两份午夜附近的存量记录时（一份带偏移、一份只有本地时间），
采用**先切换可解析记录**的策略，而不是等整组解释完再切换：

1. 每条记录的换算证据只依赖它自己的原文，带偏移的 00:05 证据完整，
   可以独立确认；本地 23:55 在来源上就无法唯一映射，整组等待只会让
   已确认记录被一条可能永远无法确认的记录无限期阻塞；
2. 未决记录只进迁移未决清单，不进绝对时间序列，先切换不会造成
   时区不明的时间混入已确认序列；
3. 已确认证据不会被后续回填失败的重试改写，先切换的结论保持稳定；
4. 旧清单两天各自的计数通过 legacy_order（原文 + 旧清单序号）完整保留，
   新旧口径的差异显式呈现，而不是静默合并。

兼容数据库（SQLite）同时保留原始文本与换算证据：``source_hash`` 对应换算前
的来源文本，``evidence_hash`` 对应换算后的结论与解析规则版本，换算前后都能
核实。同一操作编号重复迁移是幂等的；迁移按批次发布，某批解析失效时可以整批
回滚，旧清单的操作顺序（source_seq）始终保留，回滚后仍能核对。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

PARSE_RULE = "audit-time-v1"

PRECISION_ABSOLUTE = "absolute"            # 可换算为绝对时间
PRECISION_DAY = "day"                      # 可确定到日
PRECISION_LOCAL_UNRESOLVED = "local_unresolved"  # 本地时间，时区不明
PRECISION_MISSING = "missing"              # 缺失或无法解析

STATUS_CONFIRMED = "confirmed"  # 已确认：换算证据不可改写
STATUS_PENDING = "pending"      # 迁移未决：等待补全时区信息

BATCH_PUBLISHED = "published"
BATCH_ROLLED_BACK = "rolled_back"

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "audit_time_compat.db"


@dataclass(frozen=True)
class ParsedTime:
    """一条操作时间文本的换算结论。"""

    precision: str
    status: str
    utc_time: str | None
    utc_day: str | None
    local_day: str | None
    offset_text: str | None
    reason: str


def _format_offset(offset: timedelta) -> str:
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def parse_operation_time(text: Any) -> ParsedTime:
    """把旧清单里的操作时间文本换算成新版结论。

    时区不明的本地时间只标记为未决，绝不在此处猜测一个时区硬转成绝对时间。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return ParsedTime(PRECISION_MISSING, STATUS_PENDING, None, None, None, None, "操作时间为空")
    if _DATE_ONLY_RE.match(raw):
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return ParsedTime(PRECISION_MISSING, STATUS_PENDING, None, None, None, None, f"日期文本无效：{raw}")
        return ParsedTime(PRECISION_DAY, STATUS_CONFIRMED, None, None, day.isoformat(), None, "仅日期文本，可确定到日")
    candidate = raw
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return ParsedTime(PRECISION_MISSING, STATUS_PENDING, None, None, None, None, f"时间文本无法解析：{raw}")
    offset = moment.utcoffset()
    if offset is None:
        return ParsedTime(
            PRECISION_LOCAL_UNRESOLVED,
            STATUS_PENDING,
            None,
            None,
            moment.date().isoformat(),
            None,
            "本地时间缺少时区偏移，无法唯一映射到绝对时间",
        )
    utc_moment = moment.astimezone(timezone.utc)
    return ParsedTime(
        PRECISION_ABSOLUTE,
        STATUS_CONFIRMED,
        utc_moment.isoformat(),
        utc_moment.date().isoformat(),
        moment.date().isoformat(),
        _format_offset(offset),
        "带时区偏移，可换算为绝对时间",
    )


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_hash(op_id: str, original_text: str) -> str:
    """换算前来源的指纹：操作编号 + 原始时间文本。"""
    return _sha256(f"source|{op_id}|{original_text}")


def evidence_hash(op_id: str, original_text: str, parsed: ParsedTime) -> str:
    """换算后证据的指纹：解析规则版本 + 来源 + 换算结论。"""
    return _sha256(
        "evidence|{rule}|{op}|{text}|{precision}|{utc}|{utc_day}|{local_day}".format(
            rule=PARSE_RULE,
            op=op_id,
            text=original_text,
            precision=parsed.precision,
            utc=parsed.utc_time or "",
            utc_day=parsed.utc_day or "",
            local_day=parsed.local_day or "",
        )
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'published',
    item_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    rolled_back_at TEXT
);
CREATE TABLE IF NOT EXISTS time_records (
    op_id TEXT PRIMARY KEY,          -- 操作编号（旧清单的记录编号）
    source_seq INTEGER NOT NULL,     -- 旧清单里的操作顺序
    original_text TEXT NOT NULL,     -- 原始时间文本（保留原文）
    source_hash TEXT NOT NULL,       -- 换算前来源指纹
    precision TEXT NOT NULL,         -- absolute / day / local_unresolved / missing
    status TEXT NOT NULL,            -- confirmed / pending
    utc_time TEXT,                   -- 换算出的 UTC 绝对时间（仅 absolute）
    utc_day TEXT,                    -- 绝对时间所属的 UTC 日
    local_day TEXT,                  -- 原文可确定的日
    offset_text TEXT,                -- 原文携带的时区偏移
    reason TEXT NOT NULL DEFAULT '', -- 分级理由 / 未决原因
    parse_rule TEXT NOT NULL,        -- 解析规则版本
    evidence_hash TEXT NOT NULL,     -- 换算后证据指纹
    batch_id TEXT NOT NULL,          -- 首次发布该记录的批次
    published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_time_records_batch ON time_records(batch_id);
CREATE INDEX IF NOT EXISTS idx_time_records_status ON time_records(status, published);
"""


class AuditTimeMigrationStore:
    """迁移环境里的兼容数据库：新版时间记录 + 批次 + 证据。

    旧读取方式不受影响：这里只新增一份并行的换算结果，原始文本与旧清单
    顺序（source_seq）都原样保留，旧清单仍按原文读取。
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        path = str(db_path or os.environ.get("AUDIT_TIME_COMPAT_DB") or DEFAULT_DB_PATH)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.db_path = path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 迁移写入 ----------

    def migrate(self, entries: Iterable[dict[str, Any]], *, batch_id: str | None = None, note: str = "") -> dict[str, Any]:
        """把一批旧清单条目换算成新版记录。

        每条 entry 需要 ``op_id``（操作编号）、``source_seq``（旧清单顺序）、
        ``original_text``（操作时间原文）。幂等规则：

        - 同一操作编号、同一原文重复迁移：不变（unchanged）；
        - 原文变了但已有记录是已确认：保留已确认证据，记冲突（conflict_kept）；
        - 原文变了且已有记录仍未决：按新原文重算（updated，回填重试路径）；
        - 已回滚的记录重新迁移：以新批次重新发布（republished）。
        """
        batch_id = batch_id or f"BATCH-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
        now = _utcnow()
        items: list[dict[str, Any]] = []
        summary = {"inserted": 0, "unchanged": 0, "updated": 0, "republished": 0, "conflict_kept": 0}
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO batches(batch_id, note, status, item_count, created_at) VALUES (?,?,?,0,?)",
                (batch_id, note, BATCH_PUBLISHED, now),
            )
            for entry in entries:
                op_id = str(entry["op_id"]).strip()
                if not op_id:
                    raise ValueError("迁移条目缺少操作编号")
                source_seq = int(entry["source_seq"])
                original_text = "" if entry.get("original_text") is None else str(entry.get("original_text"))
                parsed = parse_operation_time(original_text)
                outcome = self._upsert(op_id, source_seq, original_text, parsed, batch_id, now)
                summary[outcome] += 1
                items.append({
                    "op_id": op_id,
                    "source_seq": source_seq,
                    "original_text": original_text,
                    "precision": parsed.precision,
                    "status": parsed.status,
                    "utc_time": parsed.utc_time,
                    "utc_day": parsed.utc_day,
                    "local_day": parsed.local_day,
                    "reason": parsed.reason,
                    "outcome": outcome,
                })
            self._conn.execute(
                "UPDATE batches SET item_count = (SELECT COUNT(*) FROM time_records WHERE batch_id = ?) WHERE batch_id = ?",
                (batch_id, batch_id),
            )
            self._conn.commit()
        return {"batch_id": batch_id, "summary": summary, "items": items}

    def _upsert(self, op_id: str, source_seq: int, original_text: str, parsed: ParsedTime, batch_id: str, now: str) -> str:
        row = self._conn.execute("SELECT * FROM time_records WHERE op_id = ?", (op_id,)).fetchone()
        s_hash = source_hash(op_id, original_text)
        e_hash = evidence_hash(op_id, original_text, parsed)
        if row is None:
            self._conn.execute(
                """INSERT INTO time_records(
                       op_id, source_seq, original_text, source_hash, precision, status,
                       utc_time, utc_day, local_day, offset_text, reason, parse_rule,
                       evidence_hash, batch_id, published, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    op_id, source_seq, original_text, s_hash, parsed.precision, parsed.status,
                    parsed.utc_time, parsed.utc_day, parsed.local_day, parsed.offset_text,
                    parsed.reason, PARSE_RULE, e_hash, batch_id, now, now,
                ),
            )
            return "inserted"
        if not row["published"]:
            # 回滚之后重新迁移：按当前原文重算证据，挂到新批次重新发布。
            self._conn.execute(
                """UPDATE time_records SET source_seq=?, original_text=?, source_hash=?, precision=?,
                       status=?, utc_time=?, utc_day=?, local_day=?, offset_text=?, reason=?,
                       parse_rule=?, evidence_hash=?, batch_id=?, published=1, updated_at=?
                   WHERE op_id=?""",
                (
                    source_seq, original_text, s_hash, parsed.precision, parsed.status,
                    parsed.utc_time, parsed.utc_day, parsed.local_day, parsed.offset_text,
                    parsed.reason, PARSE_RULE, e_hash, batch_id, now, op_id,
                ),
            )
            return "republished"
        if row["original_text"] == original_text:
            # 同一操作编号、同一原文重复迁移：幂等，不动任何已确认或未决的证据。
            return "unchanged"
        if row["status"] == STATUS_CONFIRMED:
            # 已确认的时区换算证据不可被回填重试改写。
            return "conflict_kept"
        # 回填失败后的重试：未决记录允许按新原文重算，批次保持首次发布的批次。
        self._conn.execute(
            """UPDATE time_records SET source_seq=?, original_text=?, source_hash=?, precision=?,
                   status=?, utc_time=?, utc_day=?, local_day=?, offset_text=?, reason=?,
                   parse_rule=?, evidence_hash=?, updated_at=?
               WHERE op_id=?""",
            (
                source_seq, original_text, s_hash, parsed.precision, parsed.status,
                parsed.utc_time, parsed.utc_day, parsed.local_day, parsed.offset_text,
                parsed.reason, PARSE_RULE, e_hash, now, op_id,
            ),
        )
        return "updated"

    # ---------- 批次回滚 ----------

    def rollback(self, batch_id: str) -> dict[str, Any]:
        """回滚一个批次：该批发布的记录撤出快照视图，证据保留可核对。"""
        now = _utcnow()
        with self._lock:
            batch = self._conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if batch is None:
                return {"ok": False, "message": f"批次 {batch_id} 不存在", "batch_id": batch_id, "withdrawn": 0}
            if batch["status"] == BATCH_ROLLED_BACK:
                return {"ok": True, "message": f"批次 {batch_id} 已回滚，无需重复操作", "batch_id": batch_id, "withdrawn": 0}
            cursor = self._conn.execute(
                "UPDATE time_records SET published = 0, updated_at = ? WHERE batch_id = ? AND published = 1",
                (now, batch_id),
            )
            withdrawn = cursor.rowcount
            self._conn.execute(
                "UPDATE batches SET status = ?, rolled_back_at = ? WHERE batch_id = ?",
                (BATCH_ROLLED_BACK, now, batch_id),
            )
            self._conn.commit()
        return {"ok": True, "message": f"批次 {batch_id} 已回滚，{withdrawn} 条记录撤出发布", "batch_id": batch_id, "withdrawn": withdrawn}

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id, note, status, item_count, created_at, rolled_back_at FROM batches ORDER BY created_at, batch_id"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 读取：兼容索引 / 快照视图 / 旧清单顺序 ----------

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(f"SELECT * FROM time_records {where}", params).fetchall()

    def get_record(self, op_id: str) -> dict[str, Any] | None:
        rows = self._rows("WHERE op_id = ?", (op_id,))
        return dict(rows[0]) if rows else None

    def compat_index(self) -> list[dict[str, Any]]:
        """审计时间兼容索引：当前发布中的新版记录，按操作编号关联。"""
        index: list[dict[str, Any]] = []
        for row in self._rows("WHERE published = 1 ORDER BY source_seq, op_id"):
            index.append({
                "记录编号": row["op_id"],
                "操作时间原文": row["original_text"],
                "精度": row["precision"],
                "状态": row["status"],
                "绝对时间": row["utc_time"],
                "归属日": row["utc_day"] if row["precision"] == PRECISION_ABSOLUTE else row["local_day"],
                "时区偏移": row["offset_text"],
                "未决原因": row["reason"] if row["status"] == STATUS_PENDING else "",
                "解析规则": row["parse_rule"],
                "来源哈希": row["source_hash"],
                "证据哈希": row["evidence_hash"],
                "批次": row["batch_id"],
                "旧清单序号": row["source_seq"],
            })
        return index

    def daily_sequence(self) -> list[dict[str, Any]]:
        """日序列：仅已确认记录参与。

        绝对时间记录按 UTC 日归集、按绝对时间排序；到日记录按原文的日归集、
        按旧清单序号排序。时区不明的记录一律不进日序列。
        """
        confirmed = self._rows("WHERE published = 1 AND status = ? ORDER BY source_seq", (STATUS_CONFIRMED,))
        buckets: dict[str, list[tuple[tuple[Any, ...], sqlite3.Row]]] = {}
        for row in confirmed:
            if row["precision"] == PRECISION_ABSOLUTE:
                day = row["utc_day"]
                sort_key: tuple[Any, ...] = (0, row["utc_time"], row["source_seq"])
            else:
                day = row["local_day"]
                sort_key = (1, f"{row['source_seq']:012d}", row["source_seq"])
            buckets.setdefault(day, []).append((sort_key, row))
        sequence: list[dict[str, Any]] = []
        for day in sorted(buckets):
            items = []
            for _, row in sorted(buckets[day], key=lambda pair: pair[0]):
                items.append({
                    "记录编号": row["op_id"],
                    "精度": row["precision"],
                    "绝对时间": row["utc_time"],
                    "操作时间原文": row["original_text"],
                    "旧清单序号": row["source_seq"],
                })
            sequence.append({"day": day, "count": len(items), "items": items})
        return sequence

    def pending_list(self) -> list[dict[str, Any]]:
        """迁移未决清单：时区不明或缺失的记录，按旧清单顺序排列。"""
        pending: list[dict[str, Any]] = []
        for row in self._rows("WHERE published = 1 AND status = ? ORDER BY source_seq, op_id", (STATUS_PENDING,)):
            pending.append({
                "记录编号": row["op_id"],
                "操作时间原文": row["original_text"],
                "精度": row["precision"],
                "未决原因": row["reason"],
                "批次": row["batch_id"],
                "旧清单序号": row["source_seq"],
            })
        return pending

    def snapshot_view(self) -> dict[str, Any]:
        """数据快照视角：消费兼容索引，产出日序列 + 迁移未决清单 + 合计。"""
        daily = self.daily_sequence()
        pending = self.pending_list()
        confirmed_total = sum(bucket["count"] for bucket in daily)
        absolute_total = sum(
            1 for bucket in daily for item in bucket["items"] if item["精度"] == PRECISION_ABSOLUTE
        )
        return {
            "日序列": daily,
            "迁移未决清单": pending,
            "合计": {
                "已确认记录": confirmed_total,
                "绝对时间记录": absolute_total,
                "到日记录": confirmed_total - absolute_total,
                "未决记录": len(pending),
                "覆盖天数": len(daily),
                "日序列合计": confirmed_total,
            },
        }

    def legacy_order(self) -> list[dict[str, Any]]:
        """旧清单口径：按旧清单序号排列的原文记录，迁移与回滚都不改变它。"""
        legacy: list[dict[str, Any]] = []
        for row in self._rows("ORDER BY source_seq, op_id"):
            legacy.append({
                "旧清单序号": row["source_seq"],
                "记录编号": row["op_id"],
                "操作时间原文": row["original_text"],
                "迁移状态": "已发布" if row["published"] else "已回滚",
                "精度": row["precision"],
                "批次": row["batch_id"],
            })
        return legacy

    def legacy_day_counts(self) -> dict[str, int]:
        """旧材料口径的两天各自计数：按原文文本里的日统计，不做时区换算。"""
        counts: dict[str, int] = {}
        for row in self._rows("ORDER BY source_seq, op_id"):
            text = row["original_text"].strip()
            day = text[:10] if len(text) >= 10 else "(缺失)"
            counts[day] = counts.get(day, 0) + 1
        return counts


class AuditTimeMigrationService:
    """迁移流水线：从当前审计导出取数，逐批写入兼容数据库。"""

    def __init__(self, migration_store: AuditTimeMigrationStore | None = None) -> None:
        self.migrations = migration_store or AuditTimeMigrationStore()

    @staticmethod
    def _audit_entries() -> list[dict[str, Any]]:
        """取仓库原审计导出中的时间文本：旧清单顺序就是导出顺序。"""
        from app.store import store

        entries = []
        for row in store.rows("audit"):
            entries.append({
                "op_id": str(row.get("记录编号", "")).strip(),
                "source_seq": int(row.get("id", 0)),
                "original_text": row.get("操作时间", ""),
            })
        return [entry for entry in entries if entry["op_id"]]

    def run_export_migration(self, *, note: str = "") -> dict[str, Any]:
        """把当前审计导出清单整体迁移为一个批次（重复执行幂等）。"""
        return self.migrations.migrate(self._audit_entries(), note=note or "审计导出清单整体迁移")

    def register_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """录入单条记录（手工登记 / 迟到记录），立即换算并发布。

        先切换可解析记录：每条记录独立换算、独立确认，不等待同组其他记录。
        """
        op_id = str(values.get("记录编号") or "").strip()
        if not op_id:
            return None, "缺少必填字段：记录编号"
        source_seq = values.get("旧清单序号")
        if source_seq is None:
            existing = self.migrations.legacy_order()
            source_seq = max((item["旧清单序号"] for item in existing), default=0) + 1
        result = self.migrations.migrate(
            [{"op_id": op_id, "source_seq": int(source_seq), "original_text": values.get("操作时间", "")}],
            note=f"单条录入 {op_id}",
        )
        item = result["items"][0]
        message = (
            f"记录 {op_id} 已换算：{item['reason']}"
            if item["outcome"] != "conflict_kept"
            else f"记录 {op_id} 已存在已确认的换算证据，新文本未覆盖"
        )
        return {"batch_id": result["batch_id"], **item}, message

    def rollback_batch(self, batch_id: str) -> tuple[bool, str]:
        result = self.migrations.rollback(batch_id)
        return bool(result["ok"]), str(result["message"])
