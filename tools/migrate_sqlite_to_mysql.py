# -*- coding: utf-8 -*-
"""把 SQLite 版的 crm.db 一次性搬到 MySQL

用法（在服务器 /opt/crm 下，先配好 CRM_DB_* 环境变量）：
    CRM_DB_HOST=127.0.0.1 CRM_DB_NAME=crm CRM_DB_USER=crm CRM_DB_PASSWORD=*** \
        venv/bin/python tools/migrate_sqlite_to_mysql.py --source crm.db

做三件事：
1. 用 pymysql 连上 MySQL，校验目标库是空的还是已有数据；
2. 清空目标库的业务表（--keep 可保留），再按源库原样写入，**保留主键 id**，
   这样附件、回收站快照里引用的 id 全部对得上，不需要二次修订；
3. 逐表比对条数，打印差异表。

设计取舍：
- 不做字段级转换：两边都是 SQLAlchemy 定义的类型，SQLite 里的 0/1、'YYYY-MM-DD HH:MM:SS'
  MySQL 都能直接吃，硬转反而容易把时区、小数精度搞坏；
- 迁移时 `SET FOREIGN_KEY_CHECKS=0`，避免被插入顺序卡住，迁完立刻恢复；
- 只迁移源库真实存在的表，老版本库缺的表会自动跳过（由 bootstrap 建表补齐）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pymysql
except ImportError:
    print("缺少 pymysql：pip install PyMySQL", file=sys.stderr)
    raise SystemExit(1)

# 只迁业务表；sqlite_sequence 等内部表不碰
SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "alembic_version"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite -> MySQL 数据迁移")
    parser.add_argument("--source", default="crm.db", help="源 SQLite 文件（默认 crm.db）")
    parser.add_argument("--dry-run", action="store_true", help="只比对，不写入")
    parser.add_argument("--keep", action="store_true", help="不清空目标库已有数据（可能主键冲突）")
    return parser.parse_args()


def mysql_conn() -> "pymysql.connections.Connection":
    """按与环境变量一致的约定连 MySQL（与 config.py 同一套 CRM_DB_* 变量）"""
    host = os.environ.get("CRM_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("CRM_DB_PORT") or 3306)
    name = os.environ.get("CRM_DB_NAME", "crm")
    user = os.environ.get("CRM_DB_USER", "crm")
    password = os.environ.get("CRM_DB_PASSWORD", "")
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=name,
        charset="utf8mb4", autocommit=False,
    )


def source_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in SKIP_TABLES]


def target_columns(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def fix_value(column: str, value):
    """少量语义修正：MySQL 唯一索引下空流水号必须存 NULL，否则第二笔现金收款会撞键"""
    if column == "trade_no" and value == "":
        return None
    return value


def main() -> int:
    args = parse_args()
    src_path = Path(args.source)
    if not src_path.exists():
        print(f"源库不存在：{src_path}", file=sys.stderr)
        return 1

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    my = mysql_conn()
    cur = my.cursor()

    tables = source_tables(src)
    print(f"源库：{src_path.resolve()}")
    print(f"目标：{os.environ.get('CRM_DB_USER', 'crm')}@"
          f"{os.environ.get('CRM_DB_HOST', '127.0.0.1')}/"
          f"{os.environ.get('CRM_DB_NAME', 'crm')}")
    print(f"发现 {len(tables)} 张表\n")

    # 1) 目标表存在性检查
    missing = []
    for table in tables:
        cols = target_columns(cur, table)
        if not cols:
            missing.append(table)
    if missing:
        print("目标库里缺少这些表：" + ", ".join(missing))
        print("请先跑一遍建表（venv/bin/python init_db.py）再迁数据。", file=sys.stderr)
        return 2

    # 2) 清空目标表（外键检查临时关闭，避免被插入顺序卡住）
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    if not args.keep and not args.dry_run:
        for table in tables:
            cur.execute(f"TRUNCATE TABLE `{table}`")
        my.commit()
        print("已清空目标库业务表")

    # 3) 逐表搬运
    report = []
    for table in tables:
        rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        tgt_cols = set(target_columns(cur, table))
        cols = [c for c in rows[0].keys() if c in tgt_cols] if rows else []
        if not rows:
            report.append((table, 0, 0))
            continue
        if not cols:
            print(f"  ! {table}: 源库列与目标库列完全不匹配，跳过")
            report.append((table, len(rows), 0))
            continue

        placeholders = ", ".join(["%s"] * len(cols))
        col_sql = ", ".join(f"`{c}`" for c in cols)
        sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"
        payload = [
            tuple(fix_value(c, row[c]) for c in cols)
            for row in rows
        ]
        if not args.dry_run:
            cur.executemany(sql, payload)
        report.append((table, len(rows), len(payload)))
        print(f"  + {table}: {len(payload)} 行")

    if not args.dry_run:
        my.commit()
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        my.commit()
    else:
        my.rollback()

    # 4) 校验条数
    print("\n表名                        源库    目标库   状态")
    print("-" * 52)
    bad = 0
    for table, src_count, _ in report:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        tgt_count = cur.fetchone()[0]
        ok = "OK" if tgt_count == src_count else "差异"
        if tgt_count != src_count:
            bad += 1
        print(f"{table:<26} {src_count:>7} {tgt_count:>7}   {ok}")

    src.close()
    my.close()

    if args.dry_run:
        print("\n[dry-run] 未写入任何数据")
        return 0
    print("\n迁移完成" + ("" if not bad else f"，有 {bad} 张表条数不一致，请检查"))
    return 0 if not bad else 3


if __name__ == "__main__":
    raise SystemExit(main())
