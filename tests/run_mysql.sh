#!/usr/bin/env bash
# ============================================================================
# 在 MySQL 测试库上跑全部端到端用例（不碰正式库 crm）
#
# 前置（只需一次，root 执行）：
#   CREATE DATABASE IF NOT EXISTS crm_test DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
#   GRANT ALL PRIVILEGES ON crm_test.* TO 'crm'@'127.0.0.1';
#
# 用法（服务器上，先 source /opt/crm/deploy/crm.env）：
#   bash tests/run_mysql.sh
#
# 判定：五个用例集全绿即说明「换了数据库，功能一条没少」。
# ============================================================================
set -e
cd "$(dirname "$0")/.."

TEST_DB="${CRM_TEST_DB:-crm_test}"
PORT="${CRM_PORT:-8099}"
PY="venv/bin/python"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || { echo "找不到虚拟环境 python"; exit 1; }

[ -n "${CRM_DB_HOST:-}" ] || { echo "请先 source deploy/crm.env"; exit 1; }

MYSQL_BASE=(mysql -h"${CRM_DB_HOST}" -P"${CRM_DB_PORT:-3306}" -u"${CRM_DB_USER}")
export MYSQL_PWD="${CRM_DB_PASSWORD}"

echo "== 清空测试库 ${TEST_DB} =="
TRUNCATE_SQL="$("${MYSQL_BASE[@]}" -N -B -e \
  "SELECT CONCAT('TRUNCATE TABLE \`', table_name, '\`;') FROM information_schema.tables WHERE table_schema='${TEST_DB}'")"
if [ -n "$TRUNCATE_SQL" ]; then
  "${MYSQL_BASE[@]}" "$TEST_DB" -e "SET FOREIGN_KEY_CHECKS=0; ${TRUNCATE_SQL} SET FOREIGN_KEY_CHECKS=1;"
  echo "已清空 $(printf '%s\n' "$TRUNCATE_SQL" | grep -c TRUNCATE) 张表"
else
  echo "测试库还是空的，直接建表"
fi

export CRM_DB_NAME="$TEST_DB"
export CRM_PORT="$PORT"
# 用例走 http://127.0.0.1，若沿用生产环境的 Secure Cookie（HTTPS-only），
# 会话与 CSRF token 带不上来，登录会稳定 400。测试环境显式关掉。
export CRM_COOKIE_SECURE=0
mkdir -p .tmp_test
"$PY" init_db.py >/dev/null

"$PY" app.py > .tmp_test/mysql-server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

CODE=0
set +e
for suite in test_e2e test_features test_options test_ops test_extra test_trace; do
  echo ""
  echo "==== ${suite} (MySQL) ===="
  CRM_TEST_BASE="http://127.0.0.1:${PORT}" "$PY" "tests/${suite}.py"
  CODE=$?
  [ $CODE -eq 0 ] || break
done
set -e

if [ "$CODE" -ne 0 ]; then
  echo ""
  echo "---- 服务端日志（最后 40 行）----"
  tail -40 .tmp_test/mysql-server.log
fi
exit "$CODE"
