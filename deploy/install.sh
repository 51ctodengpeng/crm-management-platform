#!/usr/bin/env bash
# ============================================================================
# CRM 一键部署脚本（在目标服务器上以 root 执行，幂等可重复跑）
#
#   用法：
#     1) 把发布包解压到 /opt/crm
#     2) 准备数据库账号（只需一次）：
#          MYSQL_ROOT_PASSWORD='root密码' bash deploy/install.sh --init-db
#     3) 常规安装 / 升级：
#          bash deploy/install.sh
#
# 做的事（严格按依赖顺序）：
#     系统用户 → Python 虚拟环境 → 依赖 → DB 账号与库 → 建表与字典种子
#     → systemd 服务 → Nginx 站点 → 健康检查
# 不做的事：
#     不动同机其它站点（changning / student-system），不改全局 nginx.conf
# ============================================================================
set -euo pipefail

APP_DIR="/opt/crm"
APP_USER="crm"
VENV="$APP_DIR/venv"
ENV_FILE="$APP_DIR/deploy/crm.env"
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

INIT_DB=0
[ "${1:-}" = "--init-db" ] && INIT_DB=1

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 执行"
[ -d "$APP_DIR" ] || die "$APP_DIR 不存在，请先把发布包解压到这里"

# ---------------------------------------------------------------- 1. 系统用户
if ! id "$APP_USER" >/dev/null 2>&1; then
  log "创建系统用户 $APP_USER（不允许登录）"
  useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$APP_USER"
else
  log "用户 $APP_USER 已存在，跳过"
fi

# ------------------------------------------------------------ 2. 环境变量文件
if [ ! -f "$ENV_FILE" ]; then
  log "生成 $ENV_FILE（首次部署）"
  cp "$APP_DIR/deploy/crm.env.example" "$ENV_FILE"
  SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  DBPWD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
  ADMINPWD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')"
  sed -i "s|^CRM_SECRET_KEY=.*|CRM_SECRET_KEY=$SECRET|" "$ENV_FILE"
  sed -i "s|^CRM_DB_PASSWORD=.*|CRM_DB_PASSWORD=$DBPWD|" "$ENV_FILE"
  # 管理员初始口令随机生成，避免全网都用 admin/admin123；只在账号表为空时生效
  if grep -q '^CRM_ADMIN_PASSWORD=' "$ENV_FILE"; then
    sed -i "s|^CRM_ADMIN_PASSWORD=.*|CRM_ADMIN_PASSWORD=$ADMINPWD|" "$ENV_FILE"
  else
    echo "CRM_ADMIN_PASSWORD=$ADMINPWD" >> "$ENV_FILE"
  fi
  warn "已生成随机 SECRET_KEY 与数据库密码，见 $ENV_FILE（妥善保存，勿提交到版本库）"
  warn "管理员初始口令：$ADMINPWD（首次登录后必须修改；已存在账号时忽略）"
else
  log "复用已有 $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# ------------------------------------------------------------ 3. 数据库账号/库
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

if [ "$INIT_DB" = "1" ]; then
  [ -n "${MYSQL_ROOT_PASSWORD:-}" ] || die "请用环境变量提供 MYSQL_ROOT_PASSWORD"
  log "创建数据库 ${CRM_DB_NAME} 与账号 ${CRM_DB_USER}（已存在则改密码）"
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -h127.0.0.1 <<SQL
CREATE DATABASE IF NOT EXISTS \`${CRM_DB_NAME}\`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '${CRM_DB_USER}'@'127.0.0.1' IDENTIFIED BY '${CRM_DB_PASSWORD}';
ALTER USER '${CRM_DB_USER}'@'127.0.0.1' IDENTIFIED BY '${CRM_DB_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${CRM_DB_NAME}\`.* TO '${CRM_DB_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
  log "数据库就绪"
fi

# ------------------------------------------------------------ 4. Python 环境
if [ ! -x "$VENV/bin/python" ]; then
  log "创建虚拟环境 $VENV"
  python3 -m venv "$VENV"
fi
log "安装依赖（清华源）"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt" -i "$PIP_INDEX"

# ------------------------------------------------------- 5. 目录与权限
mkdir -p "$APP_DIR/uploads" "$APP_DIR/avatars" "$APP_DIR/backups" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/backups"

# ------------------------------------------------------- 6. 建表 / 补列 / 种子
log "初始化数据库结构（幂等：建表 + 补列 + 字典 + 默认管理员）"
cd "$APP_DIR"
set -a; . "$ENV_FILE"; set +a
"$VENV/bin/python" init_db.py

# ------------------------------------------------------------ 7. systemd
log "安装 systemd 服务"
install -m 644 "$APP_DIR/deploy/crm.service" /etc/systemd/system/crm.service
systemctl daemon-reload
systemctl enable crm >/dev/null
systemctl restart crm
sleep 2
systemctl is-active --quiet crm || { journalctl -u crm -n 40 --no-pager; die "crm 启动失败"; }
log "crm 服务已启动"

# ------------------------------------------------------------ 8. Nginx
log "安装 Nginx 站点配置"
install -m 644 "$APP_DIR/deploy/nginx-crm.conf" /etc/nginx/conf.d/crm.conf
nginx -t || die "Nginx 配置校验失败，已保留原配置未 reload"
systemctl reload nginx
log "Nginx 已 reload"

# ------------------------------------------------------------ 9. 健康检查
log "应用内健康检查"
curl -fsS --max-time 5 http://127.0.0.1:5050/healthz || die "5050 健康检查失败"
echo
log "HTTPS 入口检查（需 DNS 已指向本机）"
curl -fsS --max-time 8 --resolve "crm.powershell.com.cn:443:127.0.0.1" \
  https://crm.powershell.com.cn/healthz || warn "HTTPS 自检未通过，检查证书与 server_name"
echo
log "部署完成：https://crm.powershell.com.cn"
