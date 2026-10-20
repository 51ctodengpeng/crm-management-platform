#!/usr/bin/env bash
# CentOS7 生产环境启动脚本：Gunicorn 只监听 127.0.0.1:8000，由 Nginx 反向代理
# 用法： bash start.sh
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# 初始化数据库（幂等，已存在则不会覆盖数据）
./.venv/bin/python init_db.py

mkdir -p logs
exec ./.venv/bin/gunicorn \
  --workers 3 \
  --threads 4 \
  --worker-class gthread \
  --bind 127.0.0.1:8000 \
  --timeout 120 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log \
  --pid logs/gunicorn.pid \
  --daemon \
  "app:app"
