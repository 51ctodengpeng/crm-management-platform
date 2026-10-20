#!/usr/bin/env bash
# 在隔离的临时数据库上跑端到端自测，不污染正式数据
set -e
cd "$(dirname "$0")/.."

TMP_DIR=".tmp_test"
PORT=8099
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR/uploads"

export CRM_DB_PATH="$PWD/$TMP_DIR/crm.db"
export CRM_UPLOAD_DIR="$PWD/$TMP_DIR/uploads"
export CRM_PORT="$PORT"

.venv/Scripts/python.exe init_db.py >/dev/null 2>&1 || .venv/bin/python init_db.py >/dev/null 2>&1

PY=".venv/Scripts/python.exe"
[ -f "$PY" ] || PY=".venv/bin/python"

# 加固项离线验证（不起服务，检查金额解析 / next 白名单 / 备份包上限 / 模块拆分一致性）
echo "==== 加固项离线验证（金额边界 / 跳转白名单 / 备份包上限） ===="
$PY tests/test_hardening.py

$PY app.py > "$TMP_DIR/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 用例内部自带就绪等待（本环境可能配置了 HTTP 代理，启动竞态会返回 502）
set +e
CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_e2e.py
CODE=$?
if [ $CODE -eq 0 ]; then
  echo ""
  echo "==== 新增功能验证（权限 / 批量 / 头像 / 地区 / 自定义） ===="
  CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_features.py
  CODE=$?
fi
if [ $CODE -eq 0 ]; then
  echo ""
  echo "==== 选项管理 / 夜间模式验证 ===="
  CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_options.py
  CODE=$?
fi
if [ $CODE -eq 0 ]; then
  echo ""
  echo "==== 导入 / 回收站 / 备份 / 安全验证 ===="
  CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_ops.py
  CODE=$?
fi
if [ $CODE -eq 0 ]; then
  echo ""
  echo "==== 多联系人 / 排序区间 / 日志增强验证 ===="
  CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_extra.py
  CODE=$?
fi
if [ $CODE -eq 0 ]; then
  echo ""
  echo "==== 溯源信息 / 省市区结构化验证 ===="
  CRM_TEST_BASE="http://127.0.0.1:$PORT" $PY tests/test_trace.py
  CODE=$?
fi
set -e

if [ $CODE -ne 0 ]; then
  echo ""
  echo "---- 服务端日志（最后 40 行）----"
  tail -40 "$TMP_DIR/server.log"
fi

exit $CODE
