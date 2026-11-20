"""SQLite 持久层：目录与摘要（不存原件字节）。

并发模型
--------
* WAL 模式 + ``busy_timeout``：两个实例共享同一库文件，读者不阻塞写者；
* 发布事务使用 ``BEGIN IMMEDIATE``，跨实例写操作被数据库锁串行化，
  后到者阻塞等待后重检唯一键，因此不会分叉、不会互相覆盖。
* 无任何 UPDATE/DELETE 路径，发布后记录不可改、不可删。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from . import merkle
from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY,          -- 1 起稠密序号，叶子下标 = id - 1
    station_id   TEXT NOT NULL,
    sample_date  TEXT NOT NULL,
    order_no     TEXT NOT NULL,
    blob_sha256  TEXT NOT NULL,
    size         INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(station_id, sample_date, order_no)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    seq         INTEGER PRIMARY KEY,            -- 该检查点对应的条数
    root_hex    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- 负荷核算结果：每站日一行“现行版本”，替换即插入新版本（只追加历史）。
-- expected_id 乐观并发：提交时携带要替换的结果编号，现行编号已变 -> 409。
CREATE TABLE IF NOT EXISTS load_results (
    id                  INTEGER PRIMARY KEY,          -- 结果编号，全表单调递增
    station_id          TEXT NOT NULL,
    sample_date         TEXT NOT NULL,
    flow_order_no       TEXT NOT NULL,
    conc_order_no       TEXT NOT NULL,
    flow_blob_sha256    TEXT NOT NULL,
    conc_blob_sha256    TEXT NOT NULL,
    hourly_json         TEXT NOT NULL,                -- 24 个整点小时区间
    daily_json          TEXT NOT NULL,                -- 全日区间
    created_at          TEXT NOT NULL,
    UNIQUE(station_id, sample_date, id)               -- 同站日内编号唯一
);

CREATE INDEX IF NOT EXISTS idx_load_current
    ON load_results(station_id, sample_date, id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConflictError(Exception):
    """业务冲突：异输入重试或超出累计上限。"""


class StaleRevisionError(Exception):
    """乐观并发冲突：提交携带的要替换编号已不是现行编号。"""


class Database:
    def __init__(self, settings: Settings):
        self._settings = settings
        settings.ensure_dirs()
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._settings.db_path,
            timeout=15.0,
            isolation_level=None,  # 手动事务，显式 BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executescript(SCHEMA)
                # 零号检查点（空树根），幂等创建。
                empty = merkle.empty_root(self._settings.tree_depth).hex()
                conn.execute(
                    "INSERT OR IGNORE INTO checkpoints(seq, root_hex, created_at) "
                    "VALUES (0, ?, ?)",
                    (empty, _utcnow()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(
        self,
        station_id: str,
        sample_date: str,
        order_no: str,
        blob_sha256: str,
        size: int,
    ) -> tuple[dict, bool]:
        """串行化发布。返回 (发布记录, 是否复用既有记录)。

        调用方必须已把原件原子落盘成功后再调用本方法，
        从而保证“库中有记录 => 卷上有完整原件”。
        """
        # 双实例并发时 BEGIN IMMEDIATE 可能撞上写锁；busy_timeout 之外再有限重试。
        for attempt in range(3):
            try:
                return self._publish_once(
                    station_id, sample_date, order_no, blob_sha256, size
                )
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _publish_once(
        self,
        station_id: str,
        sample_date: str,
        order_no: str,
        blob_sha256: str,
        size: int,
    ) -> tuple[dict, bool]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM releases "
                    "WHERE station_id = ? AND sample_date = ? AND order_no = ?",
                    (station_id, sample_date, order_no),
                ).fetchone()
                if row is not None:
                    record = dict(row)
                    same = (
                        record["blob_sha256"] == blob_sha256
                        and record["size"] == size
                    )
                    conn.rollback()  # 只读复用，不产生写入
                    if not same:
                        raise ConflictError(
                            "同一站号/日期/发布单号已存在且原件内容不同，禁止改写"
                        )
                    return record, True

                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM releases"
                ).fetchone()["c"]
                if count >= self._settings.max_releases:
                    conn.rollback()
                    raise ConflictError(
                        f"累计发布已达上限 {self._settings.max_releases} 件"
                    )

                cursor = conn.execute(
                    "INSERT INTO releases(station_id, sample_date, order_no, "
                    "blob_sha256, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        station_id,
                        sample_date,
                        order_no,
                        blob_sha256,
                        size,
                        _utcnow(),
                    ),
                )
                release_id = cursor.lastrowid
                # 无删除路径，rowid 必然稠密且等于新条数。
                assert release_id == count + 1

                leaves = self._leaf_hashes_locked(conn)
                root = merkle.root_from_leaves(leaves, self._settings.tree_depth)
                conn.execute(
                    "INSERT INTO checkpoints(seq, root_hex, created_at) "
                    "VALUES (?, ?, ?)",
                    (len(leaves), root.hex(), _utcnow()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        record = self.get_release_by_id(release_id)
        return record, False

    @staticmethod
    def _leaf_hashes_locked(conn: sqlite3.Connection) -> list[bytes]:
        rows = conn.execute(
            "SELECT station_id, sample_date, order_no, blob_sha256, size "
            "FROM releases ORDER BY id ASC"
        ).fetchall()
        return [
            merkle.leaf_hash(
                r["station_id"],
                r["sample_date"],
                r["order_no"],
                r["blob_sha256"],
                r["size"],
            )
            for r in rows
        ]

    def leaf_hashes(self) -> list[bytes]:
        with self.connect() as conn:
            return self._leaf_hashes_locked(conn)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_release(self, station_id: str, sample_date: str, order_no: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE station_id = ? AND sample_date = ? "
                "AND order_no = ?",
                (station_id, sample_date, order_no),
            ).fetchone()
            return dict(row) if row else None

    def get_release_by_id(self, release_id: int):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE id = ?", (release_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_releases(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM releases ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def latest_checkpoint(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            return dict(row)

    def get_checkpoint(self, seq: int):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE seq = ?", (seq,)
            ).fetchone()
            return dict(row) if row else None

    def list_checkpoints(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints ORDER BY seq ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 负荷核算结果（每站日一行现行版本，历史只追加）
    # ------------------------------------------------------------------ #
    def get_current_load_result(self, station_id: str, sample_date: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM load_results "
                "WHERE station_id = ? AND sample_date = ? "
                "ORDER BY id DESC LIMIT 1",
                (station_id, sample_date),
            ).fetchone()
            return dict(row) if row else None

    def list_current_load_results(self) -> list[dict]:
        """每个站日的现行版本（各站日最大 id）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.* FROM load_results r "
                "JOIN (SELECT station_id, sample_date, MAX(id) AS max_id "
                "      FROM load_results GROUP BY station_id, sample_date) m "
                "ON r.id = m.max_id ORDER BY r.id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_load_result_revisions(self, station_id: str, sample_date: str):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM load_results WHERE station_id = ? AND sample_date = ? "
                "ORDER BY id ASC",
                (station_id, sample_date),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_load_result(
        self,
        station_id: str,
        sample_date: str,
        flow_order_no: str,
        conc_order_no: str,
        flow_blob_sha256: str,
        conc_blob_sha256: str,
        hourly: list[dict],
        daily: dict,
        expected_id: int | None,
    ) -> dict:
        """在串行化事务内做乐观并发检查并插入新结果版本。

        * ``expected_id`` 为 None 表示首次提交（该站日不得已有结果）；
        * 否则必须等于该站日现行编号，不一致即抛 :class:`StaleRevisionError`，
          调用方保留旧结果、向客户端报 409。
        """
        for attempt in range(3):
            try:
                return self._save_load_result_once(
                    station_id,
                    sample_date,
                    flow_order_no,
                    conc_order_no,
                    flow_blob_sha256,
                    conc_blob_sha256,
                    hourly,
                    daily,
                    expected_id,
                )
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _save_load_result_once(
        self,
        station_id,
        sample_date,
        flow_order_no,
        conc_order_no,
        flow_blob_sha256,
        conc_blob_sha256,
        hourly,
        daily,
        expected_id,
    ) -> dict:
        import json

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM load_results "
                    "WHERE station_id = ? AND sample_date = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (station_id, sample_date),
                ).fetchone()
                current_id = row["id"] if row is not None else None
                if expected_id is None:
                    if current_id is not None:
                        conn.rollback()
                        raise StaleRevisionError(
                            f"该站日已存在现行结果编号 {current_id}，"
                            "首次提交 expected_id 须留空的前提已不成立"
                        )
                elif current_id != expected_id:
                    conn.rollback()
                    raise StaleRevisionError(
                        f"负荷结果已被其他实例重算：期望替换编号 {expected_id}，"
                        f"现行编号为 {current_id}"
                    )

                cursor = conn.execute(
                    "INSERT INTO load_results(station_id, sample_date, "
                    "flow_order_no, conc_order_no, flow_blob_sha256, "
                    "conc_blob_sha256, hourly_json, daily_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        station_id,
                        sample_date,
                        flow_order_no,
                        conc_order_no,
                        flow_blob_sha256,
                        conc_blob_sha256,
                        json.dumps(hourly, ensure_ascii=False, sort_keys=True),
                        json.dumps(daily, ensure_ascii=False, sort_keys=True),
                        _utcnow(),
                    ),
                )
                new_id = cursor.lastrowid
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        record = self.get_load_result_by_id(new_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("负荷结果插入后无法回读")
        return record

    def get_load_result_by_id(self, result_id: int):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM load_results WHERE id = ?", (result_id,)
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # 启动自检：不得带着缺件记录对外服务
    # ------------------------------------------------------------------ #
    def integrity_problems(self, blob_verifier) -> list[str]:
        problems: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, blob_sha256 FROM releases ORDER BY id"
            ).fetchall()
            expected_ids = list(range(1, len(rows) + 1))
            actual_ids = [r["id"] for r in rows]
            if actual_ids != expected_ids:
                problems.append("releases 主键不稠密，存在缺口或乱序")
            for row in rows:
                if not blob_verifier(row["blob_sha256"]):
                    problems.append(
                        f"release id={row['id']} 的原件缺失或摘要不符 "
                        f"(sha256={row['blob_sha256'][:12]}...)"
                    )
            leaves = self._leaf_hashes_locked(conn)
            root = merkle.root_from_leaves(leaves, self._settings.tree_depth)
            cp_rows = conn.execute(
                "SELECT seq, root_hex FROM checkpoints ORDER BY seq"
            ).fetchall()
            cp_seqs = [r["seq"] for r in cp_rows]
            if cp_seqs != list(range(len(cp_seqs))):
                problems.append("checkpoints 序号不连续")
            latest = cp_rows[-1]
            if latest["seq"] != len(leaves) or latest["root_hex"] != root.hex():
                problems.append("最新检查点与实际叶子集合不一致")
        return problems
