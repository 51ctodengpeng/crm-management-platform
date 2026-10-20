# -*- coding: utf-8 -*-
"""数据库方言适配层：让同一套业务代码同时跑在 SQLite（桌面版）与 MySQL（服务器版）

只有真正存在方言差异的地方才需要在这里兜一层，业务模块照旧写 SQLAlchemy 表达式：

- 日期截断：SQLite 用 strftime()，MySQL 用 DATE_FORMAT()
- 标识符引号：SQLite 用双引号，MySQL 用反引号
- 部分索引：SQLite 支持 CREATE INDEX ... WHERE，MySQL 8 不支持，
  改用「空值存 NULL + 普通唯一索引」达到同样的防重复效果（MySQL 唯一索引允许多个 NULL）
- 建索引：MySQL 8 没有 CREATE INDEX IF NOT EXISTS，改为先查 information_schema
"""
from sqlalchemy import func

from extensions import db


def dialect_name() -> str:
    """当前连接使用的方言名：sqlite / mysql / ..."""
    try:
        return db.engine.dialect.name
    except Exception:                      # 应用上下文之外（如打包脚本）退回配置值
        from config import Config
        return Config.DB_BACKEND


def is_mysql() -> bool:
    return dialect_name() == "mysql"


def format_period(column, fmt: str = "%Y-%m"):
    """把日期/时间列截断成 '2026' 或 '2026-10' 这类字符串，用于按月分组统计。

    SQLite 无 date_format()，MySQL 无 strftime()，这里统一成同一语义。
    """
    if is_mysql():
        return func.date_format(column, fmt)
    return func.strftime(fmt, column)


def q(name: str) -> str:
    """按方言给标识符加引号（MySQL 反引号 / SQLite 双引号）"""
    return f"`{name}`" if is_mysql() else f'"{name}"'


def index_exists(table: str, index_name: str) -> bool:
    """索引是否已存在（MySQL 走 information_schema，SQLite 走 sqlite_master）"""
    if is_mysql():
        row = db.session.execute(
            db.text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
            ),
            {"t": table, "i": index_name},
        ).scalar()
        return bool(row)
    row = db.session.execute(
        db.text("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name = :i"),
        {"i": index_name},
    ).scalar()
    return bool(row)


def create_index(table: str, index_name: str, column: str) -> bool:
    """建普通索引（幂等），返回是否真的新建"""
    if index_exists(table, index_name):
        return False
    db.session.execute(
        db.text(f"CREATE INDEX {q(index_name)} ON {q(table)} ({q(column)})")
    )
    return True


def ensure_payment_trade_no_unique() -> str:
    """交易流水号唯一：从数据库层兜底，防止同一笔转账被重复登记 / 重复对账。

    - SQLite：部分唯一索引，空流水号（现金、未填）不参与约束
    - MySQL：普通唯一索引。MySQL 的唯一索引允许多个 NULL，所以登记时必须把
      空流水号写成 NULL（见 routes/finance.py），语义与部分索引完全一致

    返回 'created' / 'exists' / 'skipped'（历史数据已有重复流水号时跳过，由对账页提示人工处理）
    """
    table, index_name = "payment", "ux_payment_trade_no"
    if index_exists(table, index_name):
        return "exists"
    try:
        if is_mysql():
            db.session.execute(
                db.text(f"CREATE UNIQUE INDEX {q(index_name)} ON {q(table)} ({q('trade_no')})")
            )
        else:
            db.session.execute(
                db.text(
                    f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" '
                    f'ON "{table}" ("trade_no") WHERE trade_no IS NOT NULL AND trade_no <> \'\''
                )
            )
        db.session.commit()
        return "created"
    except Exception:
        db.session.rollback()
        # 历史数据里已有重复流水号时索引建不出来。这里不能静默：唯一的数据库级
        # 防重兜底失效了，得让运维从日志里看见，去人工清理重复流水号。
        try:
            from flask import current_app
            current_app.logger.warning(
                "流水号唯一索引未能创建（历史数据可能有重复 trade_no），请人工核对",
                exc_info=True,
            )
        except Exception:
            pass
        return "skipped"
