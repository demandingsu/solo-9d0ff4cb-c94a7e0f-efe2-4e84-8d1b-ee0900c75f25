"""SQLite 连接与建表。

数据库路径由环境变量 MATRIX_DB_PATH 决定，默认 data/matrix.db。
连接采用 autocommit + 显式 BEGIN IMMEDIATE，配合忙等待，保证交印事务串行生效。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("MATRIX_DB_PATH", str(BASE_DIR / "data" / "matrix.db"))

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS libraries (
        library_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        edition    TEXT NOT NULL,
        priority   INTEGER NOT NULL,
        current    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(name, edition)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS glyphs (
        glyph_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER NOT NULL REFERENCES libraries(library_id),
        matrix_no  TEXT NOT NULL,
        glyph_char TEXT NOT NULL,
        UNIQUE(library_id, matrix_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_glyphs_char ON glyphs(glyph_char)",
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_chars (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_char   TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, glyph_char)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_matrices (
        volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_id  INTEGER NOT NULL REFERENCES glyphs(glyph_id),
        PRIMARY KEY (volume_id, glyph_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS submit_seq (seq INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_no       TEXT PRIMARY KEY,
        seq            INTEGER NOT NULL UNIQUE,
        volume_id      INTEGER NOT NULL,
        volume_name    TEXT NOT NULL,
        idem_key       TEXT,
        fingerprint    TEXT NOT NULL,
        payload_json   TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        generation     INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem
    ON orders(idem_key) WHERE idem_key IS NOT NULL
    """,
    # 清空事件：每次“清空工位”记一条。cleared_seq 是清空时已发到的序号，
    # 它随库持久化，重启后据此继续把清空前后的单号分开。
    """
    CREATE TABLE IF NOT EXISTS clears (
        clear_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        cleared_seq  INTEGER NOT NULL,
        cleared_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # 已清空单据的幂等键墓碑：旧键重试必须被识别为“该笔交印已清空”，
    # 不能命中清空后重新上传的同名册而给出新单。
    """
    CREATE TABLE IF NOT EXISTS cleared_idempotency_keys (
        idem_key    TEXT PRIMARY KEY,
        order_no    TEXT NOT NULL,
        volume_name TEXT NOT NULL,
        clear_id    INTEGER,
        cleared_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
]

# 旧库升级：orders 增加世代与清空标记信息不需要——清空时会直接删除旧单行，
# 仅保留清空事件（clears）与旧幂等键墓碑（cleared_idempotency_keys）。


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# 旧库升级：orders 增加世代标志（既有行都属于清空前的第 0 代）。
_ORDER_COLUMNS = {
    "generation": "ALTER TABLE orders ADD COLUMN generation INTEGER NOT NULL DEFAULT 0",
}


def init_db(db_path: str | None = None) -> None:
    """建表并升级旧库。

    全部语句幂等：已有的 SQLite 数据库可直接启动升级，库内既有单据照常查询和复用。
    升级时只补列，不改写既有行的内容，旧单号/旧幂等键行为保持不变。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in SCHEMA:
            conn.execute(stmt)
        columns = _column_names(conn, "orders")
        for name, ddl in _ORDER_COLUMNS.items():
            if name not in columns:
                conn.execute(ddl)
        conn.execute("INSERT OR IGNORE INTO submit_seq(seq) VALUES (0)")
        # 序号统一取 submit_seq 与订单中的最大值，避免旧库升级后回退。
        seq = conn.execute("SELECT seq FROM submit_seq").fetchone()["seq"]
        seq = max(seq, conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM orders").fetchone()["m"])
        conn.execute("UPDATE submit_seq SET seq=?", (seq,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
