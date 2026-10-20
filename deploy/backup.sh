#!/usr/bin/env bash
# ============================================================================
# CRM 数据库 + 附件冷备份（MySQL 版）
# 放置：/opt/crm/deploy/backup.sh        权限：chmod 700，属主 root
# 定时：crontab -e 加一行（每天 02:10，避开整点高峰）
#   10 2 * * * /opt/crm/deploy/backup.sh >> /var/log/crm-backup.log 2>&1
#
# 与应用内的「系统管理 → 备份与恢复」互补：
#   页面备份 = 业务人员自助，可下载、可回滚到某个时间点；
#   本脚本 = 服务器侧兜底，只落在本机 + 可选异地，超期自动清理。
# ============================================================================
set -euo pipefail

APP_DIR="/opt/crm"
BACKUP_DIR="/opt/crm/backups"
KEEP_DAYS=30
STAMP="$(date +%Y%m%d_%H%M%S)"
ENV_FILE="$APP_DIR/deploy/crm.env"

# 读取数据库连接信息（与 systemd 用同一份环境文件，避免两处口径不一致）
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

mkdir -p "$BACKUP_DIR"

# ---------- 1) 数据库：单事务导出，不锁表，业务无感 ----------
MYSQL_PWD="${CRM_DB_PASSWORD}" mysqldump \
  -h"${CRM_DB_HOST:-127.0.0.1}" -P"${CRM_DB_PORT:-3306}" -u"${CRM_DB_USER}" \
  --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF \
  --default-character-set=utf8mb4 \
  "${CRM_DB_NAME}" | gzip > "$BACKUP_DIR/crm_${STAMP}.sql.gz"

# ---------- 2) 附件与头像 ----------
tar -czf "$BACKUP_DIR/uploads_${STAMP}.tar.gz" \
  -C "$APP_DIR" uploads avatars 2>/dev/null || true

# ---------- 3) 清理超期备份 ----------
find "$BACKUP_DIR" -name "crm_*.sql.gz" -mtime +$KEEP_DAYS -delete
find "$BACKUP_DIR" -name "uploads_*.tar.gz" -mtime +$KEEP_DAYS -delete

SIZE="$(du -h "$BACKUP_DIR/crm_${STAMP}.sql.gz" | cut -f1)"
echo "[$(date '+%F %T')] 备份完成 $(basename "$BACKUP_DIR")/crm_${STAMP}.sql.gz (${SIZE})"

# ---------- 4) 异地容灾（按需启用） ----------
# rsync -az --delete "$BACKUP_DIR/" user@backup-host:/data/crm-backup/
