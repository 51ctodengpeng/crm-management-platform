# CRM 部署手册（Nginx + Gunicorn + MySQL 8）

> 目标服务器：腾讯云轻量应用服务器 `lhins-fd3befzd`（名称「测试服务器」）· 42.194.238.77 · OpenCloudOS 9.6 · 2C2G
> 访问入口：https://crm.powershell.com.cn （443 → Nginx → 127.0.0.1:5050）
> 证书：`/ssl/powershell.com.cn.pem` + `/ssl/powershell.com.cn.key`（SAN 含 `*.powershell.com.cn`，crm 子域已覆盖）

---

## 一、拓扑与关键决策

```
                      ┌──────────────────────────────────────────┐
 浏览器 ──HTTPS:443──▶ │ Nginx（/etc/nginx/conf.d/crm.conf）        │
                      │  · TLS 终止，证书读 /ssl                   │
                      │  · /static/ 直接读 /opt/crm/static         │
                      │  · /uploads/ /avatars/ 一律 403            │
                      └───────────────┬──────────────────────────┘
                                      │ HTTP 127.0.0.1:5050（仅回环）
                      ┌───────────────▼──────────────────────────┐
                      │ Gunicorn 2 worker × 4 thread（gthread）    │
                      │  Flask 应用：/opt/crm                     │
                      └───────────────┬──────────────────────────┘
                                      │ TCP 127.0.0.1:3306
                      ┌───────────────▼──────────────────────────┐
                      │ MySQL 8.0.45  库 crm  账号 crm@127.0.0.1   │
                      └──────────────────────────────────────────┘
```

**技术栈说明**：应用本体是 Flask 3.1 + SQLAlchemy，部署环境换成 MySQL 8（本地/EXE 版仍用 SQLite，同一套代码，靠 `CRM_DB_HOST` 环境变量切换）。选择该方案的原因：功能已由 283 项端到端用例覆盖，换存储层可零回归上线；换框架（Django）需要重写 ORM 与视图层约 7000 行，与本次「功能完全一致」的目标相冲突。

**同机共存**：该服务器已运行 `changning.powershell.com.cn`（5001）与 `te.powershell.com.cn`（5000）。本部署**只新增** `/opt/crm`、`crm.service`、`/etc/nginx/conf.d/crm.conf` 三项，不修改任何既有配置。

---

## 二、关键参数

| 项目 | 取值 | 说明 |
|---|---|---|
| 域名 | `crm.powershell.com.cn` | 必须有一条 A 记录指向 **42.194.238.77**（当前泛解析指向 175.178.113.227，见「四、步骤 0」） |
| 应用端口 | `127.0.0.1:5050` | 只监听回环，公网不可直连；5050 在该机空闲 |
| 对外端口 | `443`（80 仅做 301 跳转） | Lighthouse 防火墙已放通 80/443 |
| 应用目录 | `/opt/crm` | 代码 + venv + uploads + backups |
| 数据目录 | `/opt/crm`（`CRM_DATA_DIR`） | 附件 `uploads/`、头像 `avatars/`、备份 `backups/` |
| 虚拟环境 | `/opt/crm/venv` | Python 3.11.6（系统自带） |
| 运行用户 | `crm`（systemd service） | 非 root，最小权限 |
| 配置文件 | `/opt/crm/deploy/crm.env`（600） | SECRET_KEY、DB 密码全在这里，不进代码 |
| systemd 单元 | `/etc/systemd/system/crm.service` | `Type=simple`，开机自启，崩溃 5s 重启 |
| Nginx 站点 | `/etc/nginx/conf.d/crm.conf` | 与 changning 同级，互不影响 |
| 证书 | `/ssl/powershell.com.cn.pem/.key` | 泛域名，2027-01-07 到期 |
| 请求体上限 | Nginx `25m` / 应用 `20MB` | 超出 20MB 由应用给中文提示，而不是 Nginx 裸 413 |
| 数据库 | `crm` 库，`utf8mb4_0900_ai_ci`，账号 `crm@127.0.0.1` | 只授权本库，不用 root 跑应用 |
| 日志 | `journalctl -u crm`（应用）、`/var/log/nginx/crm.*.log`（访问与错误） | 应用日志走 journald，不落 `/var/log` |
| Gunicorn | 2 worker × 4 thread，`--max-requests 800` | 2G 内存，与另两个应用共存；定时回收防内存爬升 |

---

## 三、部署顺序（为什么是这个顺序）

依赖关系决定顺序，颠倒会卡在「连不上库」或「端口没人听」：

| # | 阶段 | 前置条件 | 失败后果 |
|---|---|---|---|
| 0 | **DNS 解析** | 无 | 证书没问题但域名指到别处，HTTPS 直接访问到另一台机器 |
| 1 | **系统准备**：建 `crm` 用户、目录 | root 权限 | 后面所有文件的属主都错 |
| 2 | **MySQL 建库建账号** | root 数据库口令 | 应用启动即报 `Access denied` |
| 3 | **代码落地 + venv + 依赖** | 发布包已上传 | 缺 PyMySQL 会报 `No module named pymysql` |
| 4 | **建表 / 补列 / 种子数据**（`init_db.py`） | 第 2、3 步完成 | 表不存在，首页 500 |
| 5 | **systemd 起应用**，验证 `5050/healthz` | 第 4 步完成 | Nginx 反代 502 |
| 6 | **Nginx 站点 + reload**，验证 `443` | 第 5 步已在监听 | reload 失败会连带影响同机其它站点，所以放最后且先 `nginx -t` |
| 7 | **数据迁移**（可选，从 SQLite 搬） | 第 4 步完成 | 迁移失败可重跑，源库只读不改 |
| 8 | **验收** | 全部完成 | — |

顺序铁律：**先库、再应用、最后反代**。反代是唯一「配错会波及别的站点」的环节，放最后且必须 `nginx -t` 通过再 reload。

---

## 四、服务器操作步骤

以下命令均在 42.194.238.77 上以 root 执行，可整段复制。

### 步骤 0：DNS（在 DNSPod 控制台，不在服务器上）

新增记录：`crm` → A → `42.194.238.77`，TTL 600。
验证（在服务器上执行，应返回 42.194.238.77）：

```bash
getent hosts crm.powershell.com.cn
```

> 现状：`*.powershell.com.cn` 泛解析指向 175.178.113.227（另一台 WEB 主机），
> 所以**必须**显式加这条 crm 记录，否则域名永远落在别处。

### 步骤 1：系统准备

```bash
id crm >/dev/null 2>&1 || useradd --system --home-dir /opt/crm --shell /sbin/nologin crm
mkdir -p /opt/crm/{uploads,avatars,backups,logs}
```

### 步骤 2：MySQL 建库建账号（只需一次）

```bash
# 密码请换成强密码；同样的值要写进 /opt/crm/deploy/crm.env
mysql -uroot -p <<'SQL'
CREATE DATABASE IF NOT EXISTS crm DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'crm'@'127.0.0.1' IDENTIFIED BY '在这里填数据库密码';
GRANT ALL PRIVILEGES ON crm.* TO 'crm'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

# 验证
mysql -ucrm -p -h127.0.0.1 -e "SHOW DATABASES;" | grep crm
```

关键参数：字符集必须 `utf8mb4`（中文公司名 + emoji 备注），认证插件 MySQL 8 默认 `caching_sha2_password`，PyMySQL 需 `cryptography`（已在 requirements.txt 中）。

### 步骤 3：代码落地 + 依赖

```bash
cd /opt/crm
# 发布包 crm-release.tar.gz 解压（覆盖式，不删 uploads/avatars/backups）
tar -xzf /tmp/crm-release.tar.gz -C /opt/crm

# 虚拟环境 + 依赖（清华源，约 1 分钟）
python3 -m venv /opt/crm/venv
/opt/crm/venv/bin/pip install -q --upgrade pip
/opt/crm/venv/bin/pip install -q -r /opt/crm/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 步骤 4：环境变量文件

```bash
cp /opt/crm/deploy/crm.env.example /opt/crm/deploy/crm.env
# 生成随机 SECRET_KEY
SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
sed -i "s|^CRM_SECRET_KEY=.*|CRM_SECRET_KEY=$SECRET|" /opt/crm/deploy/crm.env
# 再把 CRM_DB_PASSWORD 改成步骤 2 里设的密码
vi /opt/crm/deploy/crm.env
chmod 600 /opt/crm/deploy/crm.env
chown -R crm:crm /opt/crm
```

必改项：`CRM_SECRET_KEY`（随机）、`CRM_DB_PASSWORD`（与库一致）。
固定项：`CRM_BEHIND_PROXY=1`、`CRM_COOKIE_SECURE=1`（有 HTTPS 才能这么开）、`CRM_DATA_DIR=/opt/crm`。

### 步骤 5：建表与种子

```bash
cd /opt/crm
set -a; . /opt/crm/deploy/crm.env; set +a
/opt/crm/venv/bin/python init_db.py
```

输出应包含建表与「默认管理员 admin / admin123」。**首次登录后立刻改密码**（系统强制 8 位以上且含字母数字）。

### 步骤 6：起应用（systemd）

```bash
install -m 644 /opt/crm/deploy/crm.service /etc/systemd/system/crm.service
systemctl daemon-reload
systemctl enable --now crm
sleep 2
systemctl status crm --no-pager | head -12
curl -sS http://127.0.0.1:5050/healthz
```

期望返回：`{"status":"ok","db":"mysql",...}`。若 502 或启动失败：`journalctl -u crm -n 50 --no-pager`。

> **unit 里为什么没有 `--pid` / `--access-logfile`**：`crm` 是系统用户，写不了 `/run` 和 `/var/log`，
> 早期版本的 unit 直接因此起不来（`PermissionError: /run/tmpoj_xxx`、`'/var/log/crm-error.log' isn't writable`）。
> 现在的做法是**日志全部走 journald、pid 交给 systemd**，零权限依赖。

> **首次上线可一键完成**：脚本已放在 `/opt/crm/deploy/go_live.sh`，
> 依次做「建库建账号 → 建表 → 启动 → 健康检查 → reload nginx」，只需交互输入 MySQL root 密码：
> ```bash
> bash /opt/crm/deploy/go_live.sh
> ```
> 密码写在 `crm.env` 的 `CRM_DB_PASSWORD`，脚本读取后直接用于 `CREATE USER`，不需要手工抄写。

### 步骤 7：Nginx 站点与证书

```bash
install -m 644 /opt/crm/deploy/nginx-crm.conf /etc/nginx/conf.d/crm.conf
nginx -t                     # 必须先过这一关，失败就停下，不要 reload
systemctl reload nginx
```

同一份配置里：`ssl_certificate /ssl/powershell.com.cn.pem`、`ssl_certificate_key /ssl/powershell.com.cn.key`、`proxy_pass http://crm_app`（upstream 指向 `127.0.0.1:5050`）。

### 步骤 8：数据迁移（可选，把桌面的 crm.db 搬上云）

```bash
cd /opt/crm
set -a; . /opt/crm/deploy/crm.env; set +a
/opt/crm/venv/bin/python tools/migrate_sqlite_to_mysql.py --source /opt/crm/crm.db --dry-run   # 先看条数
/opt/crm/venv/bin/python tools/migrate_sqlite_to_mysql.py --source /opt/crm/crm.db             # 正式导
```

脚本保留主键 id，附件与回收站快照的引用不会错位；逐表比对条数，不一致会返回非零退出码。

### 步骤 9：验收

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://crm.powershell.com.cn/login
curl -sS https://crm.powershell.com.cn/healthz
echo | openssl s_client -connect crm.powershell.com.cn:443 -servername crm.powershell.com.cn 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

浏览器登录 `https://crm.powershell.com.cn`，用 admin 走一遍「客户 → 项目 → 收款 → 对账 → 导出」主链路。

---

## 五、功能验收清单（对应原系统 16 个模块）

| # | 检查项 | 判定方式 |
|---|---|---|
| 1 | 登录 / 注册 / 审批 / 锁定 | 错误密码 5 次后锁定 15 分钟，管理员可解锁 |
| 2 | 经营概览看板 | 卡片金额、近 6 月签约/回款趋势、账龄、负责人排行有数 |
| 3 | 客户管理 | 列表筛选/排序/区间、查重、多联系人、主档同步 |
| 4 | 项目管理 | 含税/不含税双向换算（6% 默认）、状态流转、项目级授权 |
| 5 | 附件 | 上传≤20MB、分类、版本归并、Office 预览、鉴权下载 |
| 6 | 收款与对账 | 转账必须流水号且唯一、超收拦截、撤销对账要 delete 权限 |
| 7 | 回款计划 / 待办 | 期次提醒、到期与逾期统计 |
| 8 | 跟进记录 | 客户/项目双归属、时间线、下次跟进 |
| 9 | 批量导入 | 模板下载 → 预览逐行校验 → 提交（≤500 行） |
| 10 | 导出 | 客户/项目台账 Excel 中文表头、带筛选条件 |
| 11 | 回收站 | 删除进快照、恢复原样（含期次映射）、彻底删除清磁盘 |
| 12 | 备份与恢复 | 页面备份生成 zip（含 mysqldump 快照）、恢复前自动预备份 |
| 13 | 选项管理 | 8 类字典增删改排、语义标记（转账/状态色）跨表生效 |
| 14 | 操作日志 | 按对象/操作人/时间区间筛选 |
| 15 | 夜间模式 | 右上角切换，刷新后保持 |
| 16 | 权限隔离 | 普通成员看不到未授权项目（换账号实测） |

> 自动化：仓库自带 283 项端到端用例（`tests/run_isolated.sh`），换库后已在本地全绿；
> 服务器上可指向 MySQL 测试库再跑一遍做双保险。

---

## 六、故障排查

| 现象 | 最可能原因 | 排查命令 |
|---|---|---|
| 502 Bad Gateway | 应用没起来 / 端口不对 | `systemctl status crm`、`ss -lntp \| grep 5050` |
| 443 返回 **400** | `conf.d/crm.conf` 已落盘但 Nginx 没 reload，不认识该 server_name | `nginx -t && systemctl reload nginx`（400 → 502 说明反代已通） |
| 启动报 `latin-1 codec can't encode` | `crm.env` 的 `CRM_DB_PASSWORD` 还是中文占位符 | `openssl rand -hex 16` 重写该行 |
| 服务起不来 + `/run/tmpXXXX` 或 `isn't writable` | unit 里的 `--pid`/`--log-file` 指向系统目录 | 用现成 `deploy/crm.service`（走 journald、无 pid 文件） |
| 回收站恢复后 URL 里的 id 变了 | 快照重建不沿用原主键（MySQL AUTO_INCREMENT 不复用已删值） | 属预期，用列表/搜索重新进入即可 |
| 500 + `Access denied` | `crm.env` 密码与库不一致 | `mysql -ucrm -p -h127.0.0.1 -e 'select 1'` |
| 500 + `No module named pymysql` | 依赖没装全 | `/opt/crm/venv/bin/pip install -r requirements.txt` |
| 静态样式丢失 | `/static/` alias 路径不对 | `ls /opt/crm/static/css/style.css` |
| 上传报 413 | 上传超 20MB（应用限制） | 正常提示；如 Nginx 先拦，检查 `client_max_body_size 25m` |
| 登录后立刻退出 | Cookie Secure 但走了 http | 确认访问的是 `https://`；`CRM_COOKIE_SECURE=1` 时 http 不保留会话 |
| 出现 `http://` 绝对链接 | 没开代理头 | `CRM_BEHIND_PROXY=1` 且 Nginx 传 `X-Forwarded-Proto` |
| 域名打开是别家站点 | DNS 未加 crm 记录，命中泛解析 | `getent hosts crm.powershell.com.cn` |
| 日志里 IP 全是 127.0.0.1 | 代理头没传到 | 同上 |

---

## 七、回滚

```bash
# 1) 只回滚代码：重新解压上一版发布包，重启服务
systemctl restart crm

# 2) 回滚到某个业务时间点：用系统内「备份与恢复」恢复（恢复前会自动再备份一份）
# 3) 彻底拆除（不影响同机其它站点）
systemctl disable --now crm
rm -f /etc/nginx/conf.d/crm.conf && nginx -t && systemctl reload nginx
# 数据保留：/opt/crm/{uploads,avatars,backups}
```

数据库每日 02:10 由 `deploy/backup.sh` 冷备到 `/opt/crm/backups`（保留 30 天），建议一并 `crontab -e` 加：
```
10 2 * * * /opt/crm/deploy/backup.sh >> /var/log/crm-backup.log 2>&1
```

---

## 八、换库后的功能验收（283 项）

`tests/run_mysql.sh` 会在 **MySQL 测试库**（默认 `crm_test`，绝不碰正式库 `crm`）上把五个用例集跑一遍：

```bash
# 前置（只需一次）：建测试库并授权
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS crm_test DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
  GRANT ALL PRIVILEGES ON crm_test.* TO 'crm'@'127.0.0.1'; FLUSH PRIVILEGES;"

cd /opt/crm
set -a; . deploy/crm.env; set +a
bash tests/run_mysql.sh
```

判定标准：`test_e2e 97` + `test_features 52` + `test_options 33` + `test_ops 63` + `test_extra 38` = **283 项全过**。

> 脚本会 `export CRM_COOKIE_SECURE=0`：用例走 `http://127.0.0.1`，若沿用生产的 Secure Cookie
> （HTTPS-only），会话与 CSRF token 带不上来，登录会稳定 400。生产环境仍保持 `CRM_COOKIE_SECURE=1`。

## 九、忘记 MySQL root 密码

```bash
NEWPW=$(openssl rand -hex 12)
systemctl stop mysqld
mkdir -p /var/run/mysqld && chown mysql:mysql /var/run/mysqld
runuser -u mysql -- /usr/sbin/mysqld --skip-grant-tables --skip-networking \
  --socket=/var/run/mysqld/reset.sock --pid-file=/var/run/mysqld/reset.pid \
  --datadir=/var/lib/mysql > /tmp/mysql-reset.log 2>&1 &
sleep 8
mysql --socket=/var/run/mysqld/reset.sock -uroot -e \
  "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED BY '$NEWPW';"
echo "$NEWPW" > /root/.mysql_root_new && chmod 600 /root/.mysql_root_new
mysqladmin --socket=/var/run/mysqld/reset.sock -uroot -p"$NEWPW" shutdown
sleep 3; systemctl start mysqld; cat /root/.mysql_root_new
```

> 期间 MySQL 不可用，同机其它站点约中断 1 分钟；改完把新密码记进密码管理器。
> `--skip-grant-tables` 下必须先 `FLUSH PRIVILEGES` 才能 `ALTER USER`，顺序不能反。
