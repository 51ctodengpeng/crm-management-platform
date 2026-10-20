# -*- coding: utf-8 -*-
"""整改加固项的离线单元验证（不需要起服务，秒级完成）

覆盖 2026-10-17 代码自查报告里的可离线验证项：
  1. 金额解析对 inf / nan / 超大值的收敛（不得污染合计、不得架空超收校验）
  2. 登录 next 参数的白名单（站内相对路径放行，反斜杠与绝对地址拒绝）
  3. 备份包的体积 / 条目上限
  4. 恢复互斥锁（POSIX）
  5. utils 与 dictionary 的转出口一致（拆分模块后既有 import 不受影响）
  6. 日志来源字段：访问地址已移除、客户省市的拼接与缺失规则

运行：.venv/Scripts/python.exe tests/test_hardening.py
"""
import math
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils                                     # noqa: E402
import dictionary                                # noqa: E402
from routes.auth import _safe_next               # noqa: E402
import backup_store                              # noqa: E402

PASS = FAIL = 0
FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    # ============ 1. 金额解析 ============
    print("== 1. 金额解析边界 ==")
    cases = {
        "inf": 0.0, "-inf": 0.0, "nan": 0.0, "NaN": 0.0,
        "1e400": 0.0, "": 0.0, "abc": 0.0,
        "0": 0.0, "1234.567": 1234.57, "12,345.678": 12345.68,
        "¥ 1,000": 1000.0, "-500.5": -500.5,
    }
    for raw, expect in cases.items():
        got = utils.parse_amount(raw)
        check(f"parse_amount({raw!r}) == {expect}", got == expect, f"实际 {got!r}")
    check("parse_amount 结果恒为有限数",
          all(math.isfinite(utils.parse_amount(v))
              for v in ("inf", "-inf", "nan", "1e400", "1e308", "-1e308")))
    check("超大金额被夹到业务上限 1e12", utils.parse_amount("1e308") == 1e12)

    # ============ 2. next 参数白名单 ============
    print("== 2. 登录 next 参数 ==")
    allowed = ["/", "/project/3", "/finance/?status=pending", "/customer/12#payments"]
    blocked = ["//evil.com", "/\\evil.com", "\\/evil.com", "https://evil.com",
               "http://evil.com/a", "javascript:alert(1)", "", None, "  ",
               "/a\r\nSet-Cookie: x=1"]
    for value in allowed:
        check(f"放行站内路径 {value!r}", _safe_next(value) == value)
    for value in blocked:
        check(f"拒绝 {value!r}", _safe_next(value) is None, f"实际 {_safe_next(value)!r}")

    # ============ 3. 备份包体积上限 ============
    print("== 3. 备份包规模校验 ==")
    with tempfile.TemporaryDirectory() as tmp:
        small = Path(tmp) / "small.zip"
        with zipfile.ZipFile(small, "w") as zf:
            zf.writestr("crm.db", "x" * 100)
        with zipfile.ZipFile(small) as zf:
            check("正常小备份包通过校验", backup_store.check_archive_limits(zf) is None)

        many = Path(tmp) / "many.zip"
        with zipfile.ZipFile(many, "w") as zf:
            for i in range(backup_store.MAX_MEMBERS + 5):
                zf.writestr(f"f{i}.txt", "")
        with zipfile.ZipFile(many) as zf:
            msg = backup_store.check_archive_limits(zf)
            check("条目数超限被拒绝", bool(msg) and "条目过多" in msg, f"实际 {msg!r}")

    # ============ 4. 恢复互斥 ============
    print("== 4. 恢复互斥锁 ==")
    if os.name == "nt":
        print("  [SKIP] Windows 桌面版为单进程，按设计不加跨进程锁")
    else:
        first = backup_store.acquire_restore_lock()
        check("第一次可获取恢复锁", first is not None)
        second = backup_store.acquire_restore_lock()
        check("第二次获取被拒绝（互斥生效）", second is None)
        if first is not None:
            first.close()
        third = backup_store.acquire_restore_lock()
        check("释放后可再次获取", third is not None)
        if third is not None:
            third.close()

    # ============ 5. 模块拆分后的一致性 ============
    print("== 5. utils / dictionary 转出口 ==")
    moved = ["option_items", "option_default", "option_usage", "option_extra",
             "order_with_other_last", "merge_local_choice", "status_kind",
             "status_color", "status_names_by_kind", "level_color",
             "transfer_methods", "is_transfer_method", "option_is_protected",
             "clear_option_cache", "STATUS_KIND_COLORS"]
    missing = [n for n in moved if not hasattr(utils, n)]
    check("utils 保留全部转出口", not missing, f"缺失 {missing}")
    same = all(getattr(utils, n) is getattr(dictionary, n) for n in moved)
    check("转出口与 dictionary 指向同一对象", same)
    check("纯函数行为一致（「其他」恒在末尾）",
          utils.order_with_other_last(["甲", "其他", "乙"]) == ["甲", "乙", "其他"])

    # ============ 6. 日志来源字段 ============
    print("== 6. 日志来源字段 ==")
    from models import OperationLog
    import clientinfo
    check("操作日志不再包含访问地址属性", not hasattr(OperationLog, "access_url"))
    check("clientinfo 不再提供地址脱敏函数", not hasattr(clientinfo, "scrub_url"))

    from logregion import format_region
    check("省市紧凑拼接", format_region("广东省", "深圳市") == "广东省深圳市")
    check("省市同名不重复", format_region("北京市", "北京市") == "北京市")
    check("省名含于市名时取市名", format_region("上海", "上海市") == "上海市")
    check("只有省级时原样返回", format_region("广东省", "") == "广东省")
    check("只有市级时原样返回", format_region("", "深圳市") == "深圳市")
    check("都为空时返回空串（由页面显示占位符）", format_region("", "") == "")
    check("纯空白按缺失处理", format_region("  ", " ") == "")

    print()
    print("=" * 50)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    else:
        print("加固项验证全部通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
